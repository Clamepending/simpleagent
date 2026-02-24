#!/usr/bin/env python3
"""Local-first AI chat service with optional forwarding."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List

import requests
from flask import Flask, jsonify, request


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if key:
                os.environ.setdefault(key, value.strip())


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: List[Dict[str, Any]] = []
        self.last_forward: Optional[Dict[str, Any]] = None
        self.sessions: Dict[str, List[Dict[str, Any]]] = {}

    def record_event(self, event: Dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)
            self.events = self.events[-200:]

    def set_last_forward(self, info: Dict[str, Any]) -> None:
        with self.lock:
            self.last_forward = info

    def append_session(self, session_id: str, role: str, content: str) -> None:
        with self.lock:
            history = self.sessions.setdefault(session_id, [])
            history.append({"role": role, "content": content, "ts": time.time()})
            self.sessions[session_id] = history[-100:]

    def get_session(self, session_id: str) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.sessions.get(session_id, []))

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "events": list(self.events),
                "last_forward": dict(self.last_forward) if self.last_forward else None,
                "sessions": {"session_count": len(self.sessions), "session_ids": sorted(self.sessions.keys())},
            }


def create_app() -> Flask:
    app = Flask(__name__)
    state = AppState()
    _load_dotenv_file()

    forward_enabled = _env_bool("ADMINAGENT_FORWARD_ENABLED", False)
    forward_url = os.getenv("ADMINAGENT_FORWARD_URL", "").strip()
    forward_token = os.getenv("ADMINAGENT_FORWARD_TOKEN", "").strip()
    llm_url = os.getenv("ADMINAGENT_LLM_URL", "https://api.openai.com/v1/chat/completions").strip()
    llm_api_key = os.getenv("ADMINAGENT_LLM_API_KEY", "").strip()
    llm_model = os.getenv("ADMINAGENT_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    llm_timeout_s = int(os.getenv("ADMINAGENT_LLM_TIMEOUT_S", "60"))
    llm_system_prompt = os.getenv(
        "ADMINAGENT_SYSTEM_PROMPT",
        "You are AdminAgent, a concise, practical operations assistant.",
    ).strip()

    def _forward(message: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not forward_enabled:
            return {"ok": False, "message": "forwarding disabled"}
        if not forward_url:
            raise RuntimeError("ADMINAGENT_FORWARD_URL is required when ADMINAGENT_FORWARD_ENABLED=1")

        headers = {"Content-Type": "application/json"}
        if forward_token:
            headers["Authorization"] = f"Bearer {forward_token}"
        body = {"source": "adminagent", "message": message, "event": payload, "received_at": time.time()}
        resp = requests.post(forward_url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json() if resp.text else {}
        result = {"ok": True, "forwarded_at": time.time(), "response": data}
        state.set_last_forward(result)
        return result

    def _build_llm_messages(session_id: str, user_message: str) -> List[Dict[str, str]]:
        history = state.get_session(session_id)[-20:]
        messages: List[Dict[str, str]] = [{"role": "system", "content": llm_system_prompt}]
        for item in history:
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _generate_chat_response(session_id: str, user_message: str) -> str:
        if not llm_url:
            raise RuntimeError("ADMINAGENT_LLM_URL is required for /api/chat")
        if not llm_api_key:
            raise RuntimeError("ADMINAGENT_LLM_API_KEY is required for /api/chat")

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {llm_api_key}"}
        payload = {
            "model": llm_model,
            "messages": _build_llm_messages(session_id=session_id, user_message=user_message),
            "temperature": 0.2,
        }
        resp = requests.post(llm_url, json=payload, headers=headers, timeout=llm_timeout_s)
        resp.raise_for_status()
        data = resp.json() if resp.text else {}
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM response missing choices")
        assistant_text = str((choices[0].get("message") or {}).get("content", "")).strip()
        if not assistant_text:
            raise RuntimeError("LLM response did not include assistant content")
        return assistant_text

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
    <h1>AdminAgent Chat</h1>
    <p class="muted">Chat via <code>/api/chat</code>. Session history is retained in memory by <code>session_id</code>.</p>
    <div id="status" class="status">Loading status...</div>
    <div id="chat" class="chat"></div>
    <form id="chatForm">
      <input id="sessionInput" type="text" value="local-dev" aria-label="Session ID" style="max-width: 170px;" />
      <input id="msgInput" type="text" placeholder="Type a message to test your agent..." required />
      <button type="submit">Send</button>
    </form>
    <div class="meta">
      Endpoint used: <code>/api/chat</code><br>
      Model endpoint: <code id="modelUrl">(loading)</code><br>
      Model name: <code id="modelName">(loading)</code><br>
      Model API key: <code id="modelKey">(loading)</code><br>
      Forward URL: <code id="forwardUrl">(loading)</code><br>
      Forward API key: <code id="forwardKey">(loading)</code><br>
      If forwarding is enabled, each assistant response is also forwarded.
    </div>
  </div>
  <script>
    const chat = document.getElementById("chat");
    const statusEl = document.getElementById("status");
    const forwardUrlEl = document.getElementById("forwardUrl");
    const modelUrlEl = document.getElementById("modelUrl");
    const modelNameEl = document.getElementById("modelName");
    const modelKeyEl = document.getElementById("modelKey");
    const forwardKeyEl = document.getElementById("forwardKey");
    const form = document.getElementById("chatForm");
    const sessionInput = document.getElementById("sessionInput");
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
          " | Model: " + data.model;
        forwardUrlEl.textContent = data.forward_url || "(not set)";
        modelUrlEl.textContent = data.llm_url || "(not set)";
        modelNameEl.textContent = data.model || "(not set)";
        modelKeyEl.textContent = data.llm_api_key || "(not set)";
        forwardKeyEl.textContent = data.forward_api_key || "(not set)";
      } catch (err) {
        statusEl.textContent = "Health check failed: " + err;
        modelUrlEl.textContent = "(health failed)";
        modelNameEl.textContent = "(health failed)";
        modelKeyEl.textContent = "(health failed)";
        forwardUrlEl.textContent = "(health failed)";
        forwardKeyEl.textContent = "(health failed)";
      }
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      const sessionId = (sessionInput.value || "").trim() || "local-dev";
      if (!text) return;

      addMessage("You: " + text, "user");
      input.value = "";

      try {
        const resp = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, message: text })
        });
        const data = await resp.json();
        if (resp.ok) {
          addMessage("Assistant (" + sessionId + "): " + data.response, "system");
        } else {
          addMessage("Agent error: " + JSON.stringify(data, null, 2), "error");
        }
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
                "forward_enabled": forward_enabled,
                "forward_url": forward_url,
                "forward_api_key": _mask_secret(forward_token),
                "llm_url": llm_url,
                "llm_api_key": _mask_secret(llm_api_key),
                "model": llm_model,
                "sessions": state.snapshot().get("sessions"),
            }
        )

    @app.route("/api/events", methods=["GET"])
    def events():
        return jsonify({"status": "ok", **state.snapshot()})

    @app.route("/api/sessions/<session_id>", methods=["GET"])
    def get_session(session_id: str):
        session_id = session_id.strip()
        if not session_id:
            return jsonify({"status": "error", "error": "session_id is required"}), 400
        return jsonify({"status": "ok", "session_id": session_id, "history": state.get_session(session_id)})

    @app.route("/api/chat", methods=["POST"])
    def chat():
        data = request.get_json(silent=True) or {}
        session_id = str(data.get("session_id", "")).strip() or "default"
        user_message = str(data.get("message", "")).strip()
        if not user_message:
            return jsonify({"status": "error", "error": "message is required"}), 400

        state.append_session(session_id=session_id, role="user", content=user_message)
        chat_payload = {
            "source": "chat-ui",
            "event_type": "chat_message",
            "session_id": session_id,
            "note": user_message,
            "task_description": "User chat message",
            "task_status": "active",
            "task_done": False,
        }
        event_record = {
            "received_at": time.time(),
            "path": "/api/chat",
            "session_id": session_id,
            "payload": chat_payload,
            "rendered_message": user_message,
            "forwarded": False,
        }
        try:
            assistant_text = _generate_chat_response(session_id=session_id, user_message=user_message)
            state.append_session(session_id=session_id, role="assistant", content=assistant_text)
            forward_result = _forward(
                message=assistant_text,
                payload={**chat_payload, "assistant_response": assistant_text},
            )
            event_record["forwarded"] = bool(forward_result.get("ok"))
            event_record["assistant_response"] = assistant_text
            state.record_event(event_record)
            return jsonify(
                {
                    "status": "ok",
                    "session_id": session_id,
                    "response": assistant_text,
                    "forwarded": bool(forward_result.get("ok")),
                    "forward_result": forward_result,
                }
            )
        except Exception as exc:
            state.record_event({**event_record, "error": str(exc)})
            return jsonify({"status": "error", "error": str(exc), "session_id": session_id}), 502

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "18789"))
    app.run(host=host, port=port, debug=False, threaded=True)
