#!/usr/bin/env python3
"""Minimal OpenClaw-style webhook gateway."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, jsonify, request


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv_file(path: str = ".env") -> None:
    """Load KEY=VALUE pairs into environment without overriding existing vars."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if not key:
                continue
            os.environ.setdefault(key, value.strip())


def render_template(template: str, values: Dict[str, Any]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + str(key) + "}}", str(value))
    return out


class GatewayState:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: List[Dict[str, Any]] = []
        self.last_forward: Optional[Dict[str, Any]] = None

    def record_event(self, event: Dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)
            self.events = self.events[-200:]

    def set_last_forward(self, info: Dict[str, Any]) -> None:
        with self.lock:
            self.last_forward = info

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "events": list(self.events),
                "last_forward": dict(self.last_forward) if self.last_forward else None,
            }


def create_app() -> Flask:
    app = Flask(__name__)
    state = GatewayState()
    _load_dotenv_file()

    hook_path = os.getenv("GATEWAY_HOOK_PATH", "inbox").strip("/") or "inbox"
    token = os.getenv("GATEWAY_TOKEN", "").strip()
    forward_enabled = _env_bool("ADMINAGENT_FORWARD_ENABLED", False)
    forward_url = os.getenv("ADMINAGENT_FORWARD_URL", "").strip()
    forward_token = os.getenv("ADMINAGENT_FORWARD_TOKEN", "").strip()
    message_template = os.getenv(
        "GATEWAY_MESSAGE_TEMPLATE",
        "VISION ALERT on device {{io_id}} (task {{task_id}}): {{note}}\nTask: {{task_description}}",
    )

    def _unauthorized():
        return jsonify({"status": "error", "error": "unauthorized"}), 401

    def _authorized() -> bool:
        if not token:
            return True
        auth = request.headers.get("Authorization", "")
        return auth == f"Bearer {token}"

    def _forward(message: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not forward_enabled:
            return {"ok": False, "message": "forwarding disabled"}
        if not forward_url:
            raise RuntimeError("ADMINAGENT_FORWARD_URL is required when ADMINAGENT_FORWARD_ENABLED=1")

        body = {
            "source": "adminagent",
            "message": message,
            "event": payload,
            "received_at": time.time(),
        }
        headers = {"Content-Type": "application/json"}
        if forward_token:
            headers["Authorization"] = f"Bearer {forward_token}"

        resp = requests.post(forward_url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json() if resp.text else {}
        result = {"ok": True, "forwarded_at": time.time(), "response": data}
        state.set_last_forward(result)
        return result

    def _send_discord(message: str, username: Optional[str] = None) -> Dict[str, Any]:
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook_url:
            return {"status": "error", "error": "DISCORD_WEBHOOK_URL is not configured"}
        body: Dict[str, Any] = {"content": message}
        if username:
            body["username"] = username
        resp = requests.post(webhook_url, json=body, timeout=10)
        if resp.status_code not in (200, 204):
            return {"status": "error", "error": f"Discord returned status {resp.status_code}"}
        return {"status": "ok", "channel": "discord"}

    def _send_telegram(message: str) -> Dict[str, Any]:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not bot_token:
            return {"status": "error", "error": "TELEGRAM_BOT_TOKEN is not configured"}
        if not chat_id:
            return {"status": "error", "error": "TELEGRAM_CHAT_ID is not configured"}
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        data = resp.json() if resp.text else {}
        if not resp.ok or not data.get("ok", False):
            return {"status": "error", "error": data.get("description", f"Telegram returned status {resp.status_code}")}
        return {"status": "ok", "channel": "telegram"}

    @app.route("/", methods=["GET"])
    def index():
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AdminAgent Chat Tester</title>
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
      background: #0b1020;
      color: #e8ecff;
    }
    .wrap { max-width: 860px; margin: 0 auto; padding: 24px 16px 40px; }
    h1 { margin: 0 0 10px; font-size: 24px; }
    .muted { color: #a8b0d4; margin: 0 0 16px; }
    .status { font-size: 14px; color: #9cf7c1; margin-bottom: 12px; }
    .chat {
      border: 1px solid #23315f;
      border-radius: 12px;
      background: #101935;
      height: 360px;
      overflow-y: auto;
      padding: 12px;
      margin-bottom: 12px;
    }
    .msg {
      padding: 8px 10px;
      border-radius: 8px;
      margin-bottom: 8px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .user { background: #24438d; }
    .system { background: #1d2a52; }
    .error { background: #60252b; }
    form { display: flex; gap: 8px; }
    input[type="text"] {
      flex: 1;
      background: #0f1733;
      color: #e8ecff;
      border: 1px solid #2b3f7a;
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
    }
    button {
      border: 1px solid #3e56a8;
      border-radius: 10px;
      padding: 10px 14px;
      background: #3552a6;
      color: white;
      cursor: pointer;
      font-size: 14px;
    }
    .meta {
      margin-top: 14px;
      font-size: 12px;
      color: #b3b9d6;
      line-height: 1.6;
    }
    code {
      background: #162349;
      border-radius: 6px;
      padding: 1px 6px;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>AdminAgent Chat Tester</h1>
    <p class="muted">Send a message to <code>/api/trigger</code> and inspect forwarding behavior.</p>
    <div id="status" class="status">Loading status...</div>
    <div id="chat" class="chat"></div>
    <form id="chatForm">
      <input id="msgInput" type="text" placeholder="Type a message to test your agent..." required />
      <button type="submit">Send</button>
    </form>
    <div class="meta">
      Endpoint used: <code>/api/trigger</code><br>
      Hook endpoint: <code id="hookPath">(loading)</code><br>
      Forward URL: <code id="forwardUrl">(loading)</code><br>
      Forward API key: <code id="forwardKey">(loading)</code><br>
      Incoming webhook key: <code id="gatewayKey">(loading)</code><br>
      If your downstream forwarding is enabled, each send will attempt to forward.
    </div>
  </div>
  <script>
    const chat = document.getElementById("chat");
    const statusEl = document.getElementById("status");
    const forwardUrlEl = document.getElementById("forwardUrl");
    const hookPathEl = document.getElementById("hookPath");
    const forwardKeyEl = document.getElementById("forwardKey");
    const gatewayKeyEl = document.getElementById("gatewayKey");
    const form = document.getElementById("chatForm");
    const input = document.getElementById("msgInput");

    function addMessage(text, cls) {
      const el = document.createElement("div");
      el.className = "msg " + cls;
      el.textContent = text;
      chat.appendChild(el);
      chat.scrollTop = chat.scrollHeight;
    }

    async function loadHealth() {
      try {
        const resp = await fetch("/health");
        const data = await resp.json();
        statusEl.textContent =
          "Service: " + data.service +
          " | Forward enabled: " + data.forward_enabled +
          " | Hook: " + data.hook_path;
        hookPathEl.textContent = data.hook_path || "(not set)";
        forwardUrlEl.textContent = data.forward_url || "(not set)";
        forwardKeyEl.textContent = data.forward_api_key || "(not set)";
        gatewayKeyEl.textContent = data.gateway_token || "(not set)";
      } catch (err) {
        statusEl.textContent = "Health check failed: " + err;
        forwardUrlEl.textContent = "(health failed)";
        forwardKeyEl.textContent = "(health failed)";
        gatewayKeyEl.textContent = "(health failed)";
      }
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;

      addMessage("You: " + text, "user");
      input.value = "";

      const payload = {
        source: "chat-ui",
        event_type: "manual_chat",
        task_id: "ui-manual",
        io_id: "browser",
        task_description: "Manual chat test from browser UI",
        note: text,
        task_status: "active",
        task_done: false
      };

      try {
        const resp = await fetch("/api/trigger", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, payload })
        });
        const data = await resp.json();
        addMessage("Agent result: " + JSON.stringify(data, null, 2), resp.ok ? "system" : "error");
      } catch (err) {
        addMessage("Request error: " + err, "error");
      }
    });

    loadHealth();
    addMessage("Ready. Type a message and click Send.", "system");
  </script>
</body>
</html>
"""

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify(
            {
                "status": "ok",
                "service": "adminagent",
                "hook_path": f"/hooks/{hook_path}",
                "forward_enabled": forward_enabled,
                "forward_url": forward_url,
                "forward_api_key": forward_token,
                "gateway_token": token,
                "discord_configured": bool(os.getenv("DISCORD_WEBHOOK_URL", "").strip()),
                "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip()),
            }
        )

    @app.route("/api/events", methods=["GET"])
    def events():
        return jsonify({"status": "ok", **state.snapshot()})

    @app.route("/api/trigger", methods=["POST"])
    def manual_trigger():
        data = request.get_json(silent=True) or {}
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "error": "payload must be an object"}), 400
        message = str(data.get("message", "")).strip() or render_template(message_template, payload)

        event_record = {
            "received_at": time.time(),
            "path": "/api/trigger",
            "payload": payload,
            "rendered_message": message,
            "forwarded": False,
        }
        try:
            forward_result = _forward(message, payload)
            event_record["forwarded"] = bool(forward_result.get("ok"))
            state.record_event(event_record)
            return jsonify({"status": "ok", "result": forward_result})
        except Exception as exc:
            state.record_event({**event_record, "error": str(exc)})
            return jsonify({"status": "error", "error": str(exc)}), 502

    @app.route("/api/actions/discord", methods=["POST"])
    def send_discord():
        data = request.get_json(silent=True) or {}
        message = str(data.get("message", "")).strip()
        username = str(data.get("username", "")).strip() or None
        if not message:
            return jsonify({"status": "error", "error": "message is required"}), 400
        result = _send_discord(message=message, username=username)
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code

    @app.route("/api/actions/telegram", methods=["POST"])
    def send_telegram():
        data = request.get_json(silent=True) or {}
        message = str(data.get("message", "")).strip()
        if not message:
            return jsonify({"status": "error", "error": "message is required"}), 400
        result = _send_telegram(message=message)
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code

    @app.route(f"/hooks/{hook_path}", methods=["POST"])
    def hook():
        if not _authorized():
            return _unauthorized()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "error": "Request body must be JSON object"}), 400

        message = render_template(message_template, payload)
        event_record = {
            "received_at": time.time(),
            "path": request.path,
            "payload": payload,
            "rendered_message": message,
            "forwarded": False,
        }
        try:
            forward_result = _forward(message, payload)
            event_record["forwarded"] = bool(forward_result.get("ok"))
            state.record_event(event_record)
            return jsonify(
                {
                    "status": "ok",
                    "forwarded": bool(forward_result.get("ok")),
                    "message": message,
                    "result": forward_result,
                }
            )
        except Exception as exc:
            state.record_event({**event_record, "error": str(exc)})
            return jsonify({"status": "error", "error": str(exc), "message": message}), 502

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "18789"))
    app.run(host=host, port=port, debug=False, threaded=True)
