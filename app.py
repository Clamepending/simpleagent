#!/usr/bin/env python3
"""Local-first AI chat service with optional forwarding."""

from __future__ import annotations

import os
import sqlite3
import json
import re
import subprocess
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


def _upsert_env_var(path: str, key: str, value: str) -> None:
    lines: List[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updated = False
    new_lines: List[str] = []
    for line in lines:
        text = line.strip()
        if text.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = f"{new_lines[-1]}\n"
        new_lines.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _truncate_text(value: str, max_chars: int) -> str:
    if max_chars < 1:
        return ""
    if len(value) <= max_chars:
        return value
    clipped = value[: max(1, max_chars)]
    return f"{clipped}\n...[truncated]"


class AppState:
    def __init__(self, session_store: "SqliteSessionStore"):
        self.lock = threading.Lock()
        self.events: List[Dict[str, Any]] = []
        self.last_forward: Optional[Dict[str, Any]] = None
        self.session_store = session_store

    def record_event(self, event: Dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)
            self.events = self.events[-200:]

    def set_last_forward(self, info: Dict[str, Any]) -> None:
        with self.lock:
            self.last_forward = info

    def append_session(self, session_id: str, role: str, content: str) -> None:
        self.session_store.append_message(session_id=session_id, role=role, content=content)

    def get_session(self, session_id: str) -> List[Dict[str, Any]]:
        return self.session_store.get_session(session_id=session_id)

    def list_sessions(self) -> List[Dict[str, Any]]:
        return self.session_store.list_sessions()

    def snapshot(self) -> Dict[str, Any]:
        sessions_snapshot = self.session_store.snapshot()
        with self.lock:
            return {
                "events": list(self.events),
                "last_forward": dict(self.last_forward) if self.last_forward else None,
                "sessions": sessions_snapshot,
            }


class SqliteSessionStore:
    def __init__(self, db_path: str, max_messages_per_session: int = 100):
        self.db_path = db_path.strip() or "adminagent.db"
        self.max_messages_per_session = max(1, int(max_messages_per_session))
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        if self.db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_messages_session_id_id
                ON session_messages(session_id, id)
                """
            )
            conn.commit()

    def append_message(self, session_id: str, role: str, content: str) -> None:
        ts = time.time()
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO session_messages(session_id, role, content, ts) VALUES (?, ?, ?, ?)",
                (session_id, role, content, ts),
            )
            conn.execute(
                """
                DELETE FROM session_messages
                WHERE session_id = ?
                  AND id NOT IN (
                      SELECT id
                      FROM session_messages
                      WHERE session_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (session_id, session_id, self.max_messages_per_session),
            )
            conn.commit()

    def get_session(self, session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        max_rows = self.max_messages_per_session if limit is None else max(1, int(limit))
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, ts
                FROM (
                    SELECT id, role, content, ts
                    FROM session_messages
                    WHERE session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (session_id, max_rows),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"], "ts": row["ts"]} for row in rows]

    def list_sessions(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, COUNT(*) AS message_count, MAX(ts) AS updated_at
                FROM session_messages
                GROUP BY session_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "message_count": int(row["message_count"]),
                "updated_at": float(row["updated_at"]) if row["updated_at"] is not None else None,
            }
            for row in rows
        ]

    def snapshot(self) -> Dict[str, Any]:
        with self.lock, self._connect() as conn:
            count_row = conn.execute(
                "SELECT COUNT(DISTINCT session_id) AS session_count FROM session_messages"
            ).fetchone()
            id_rows = conn.execute(
                "SELECT DISTINCT session_id FROM session_messages ORDER BY session_id ASC"
            ).fetchall()
        return {
            "session_count": int((count_row or {"session_count": 0})["session_count"]),
            "session_ids": [row["session_id"] for row in id_rows],
        }


def create_app() -> Flask:
    app = Flask(__name__)
    _load_dotenv_file()

    session_db_path = os.getenv("ADMINAGENT_DB_PATH", "adminagent.db").strip() or "adminagent.db"
    session_max_messages = int(os.getenv("ADMINAGENT_SESSION_MAX_MESSAGES", "100"))
    state = AppState(
        session_store=SqliteSessionStore(
            db_path=session_db_path,
            max_messages_per_session=session_max_messages,
        )
    )

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
    shell_enabled = _env_bool("ADMINAGENT_SHELL_ENABLED", False)
    shell_cwd = os.getenv("ADMINAGENT_SHELL_CWD", ".").strip() or "."
    shell_timeout_s = max(1, int(os.getenv("ADMINAGENT_SHELL_TIMEOUT_S", "20")))
    shell_max_output_chars = max(200, int(os.getenv("ADMINAGENT_SHELL_MAX_OUTPUT_CHARS", "8000")))
    shell_max_calls_per_turn = max(1, int(os.getenv("ADMINAGENT_SHELL_MAX_CALLS_PER_TURN", "3")))
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_poll_enabled = _env_bool("TELEGRAM_POLL_ENABLED", True)
    telegram_poll_timeout_s = max(1, min(50, int(os.getenv("TELEGRAM_POLL_TIMEOUT_S", "25"))))
    telegram_poll_retry_s = max(1, int(os.getenv("TELEGRAM_POLL_RETRY_S", "5")))
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    google_api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    gateway_token = os.getenv("GATEWAY_TOKEN", "").strip()

    shell_resolved_cwd = os.path.abspath(shell_cwd)
    if not os.path.isdir(shell_resolved_cwd):
        shell_resolved_cwd = os.getcwd()
    telegram_poll_state: Dict[str, Any] = {
        "started_at": None,
        "last_polled_at": None,
        "last_update_id": None,
        "last_error": "",
    }
    telegram_poll_lock = threading.Lock()

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

    def _telegram_token_ready() -> bool:
        return bool(telegram_bot_token) and not telegram_bot_token.startswith("replace-with-")

    def _telegram_api(method: str, payload: Dict[str, Any]) -> Any:
        if not telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for Telegram integration")
        url = f"https://api.telegram.org/bot{telegram_bot_token}/{method}"
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json() if resp.text else {}
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {json.dumps(data, ensure_ascii=True)}")
        return data.get("result")

    def _telegram_send_message(chat_id: int, text: str, reply_to_message_id: Optional[int]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return _telegram_api("sendMessage", payload)

    def _telegram_get_updates(offset: int, timeout_s: int) -> List[Dict[str, Any]]:
        result = _telegram_api(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_s,
                "allowed_updates": ["message"],
            },
        )
        return result if isinstance(result, list) else []

    def _build_llm_messages(session_id: str, user_message: str) -> List[Dict[str, str]]:
        history = state.get_session(session_id)[-20:]
        effective_system_prompt = llm_system_prompt
        if shell_enabled:
            effective_system_prompt = (
                f"{llm_system_prompt}\n\n"
                "You can request shell access when needed by responding with exactly one line in this format:\n"
                "<tool:shell>your shell command</tool:shell>\n"
                "Rules:\n"
                "- Use shell only when required to answer accurately.\n"
                "- Keep commands minimal and non-interactive.\n"
                "- After tool output is returned to you, provide a normal user-facing answer.\n"
                "- Do not wrap the tool line in markdown."
            )
        messages: List[Dict[str, str]] = [{"role": "system", "content": effective_system_prompt}]
        for item in history:
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _extract_shell_command(text: str) -> Optional[str]:
        match = re.fullmatch(r"\s*<tool:shell>(.+?)</tool:shell>\s*", text, flags=re.DOTALL)
        if not match:
            return None
        command = match.group(1).strip()
        return command or None

    def _run_shell(command: str) -> Dict[str, Any]:
        started = time.time()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=shell_resolved_cwd,
                capture_output=True,
                text=True,
                timeout=shell_timeout_s,
            )
            return {
                "ok": True,
                "command": command,
                "cwd": shell_resolved_cwd,
                "exit_code": completed.returncode,
                "stdout": _truncate_text(completed.stdout or "", shell_max_output_chars),
                "stderr": _truncate_text(completed.stderr or "", shell_max_output_chars),
                "duration_ms": int((time.time() - started) * 1000),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "command": command,
                "cwd": shell_resolved_cwd,
                "error": f"timed out after {shell_timeout_s}s",
                "stdout": _truncate_text(exc.stdout or "", shell_max_output_chars),
                "stderr": _truncate_text(exc.stderr or "", shell_max_output_chars),
                "duration_ms": int((time.time() - started) * 1000),
            }
        except Exception as exc:
            return {
                "ok": False,
                "command": command,
                "cwd": shell_resolved_cwd,
                "error": str(exc),
                "stdout": "",
                "stderr": "",
                "duration_ms": int((time.time() - started) * 1000),
            }

    def _call_llm(messages: List[Dict[str, str]]) -> str:
        if not llm_url:
            raise RuntimeError("ADMINAGENT_LLM_URL is required for /api/chat")
        if not llm_api_key:
            raise RuntimeError("ADMINAGENT_LLM_API_KEY is required for /api/chat")

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {llm_api_key}"}
        payload = {
            "model": llm_model,
            "messages": messages,
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

    def _generate_chat_response(session_id: str, user_message: str) -> str:
        messages = _build_llm_messages(session_id=session_id, user_message=user_message)
        for _ in range(shell_max_calls_per_turn + 1):
            assistant_text = _call_llm(messages)
            shell_command = _extract_shell_command(assistant_text)
            if not shell_command:
                return assistant_text
            if not shell_enabled:
                return "Shell access is disabled by configuration."

            shell_result = _run_shell(shell_command)
            messages.append({"role": "assistant", "content": assistant_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "TOOL_RESULT shell\n"
                        f"{json.dumps(shell_result, ensure_ascii=True)}\n"
                        "Now continue and answer the user directly."
                    ),
                }
            )
        return "I hit the shell tool-call limit for this turn. Please narrow the request and try again."

    def _process_telegram_message(message: Dict[str, Any], update_id: Optional[int], source_path: str) -> Dict[str, Any]:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return {"status": "ignored", "reason": "missing chat"}

        chat_id_raw = chat.get("id")
        chat_id: Optional[int] = None
        if isinstance(chat_id_raw, (int, str)) and str(chat_id_raw).strip():
            try:
                chat_id = int(chat_id_raw)
            except ValueError:
                chat_id = None
        if chat_id is None:
            return {"status": "ignored", "reason": "invalid chat id"}

        user_message = str(message.get("text", "")).strip()
        if not user_message:
            return {"status": "ignored", "reason": "only text messages are supported"}

        message_id_raw = message.get("message_id")
        reply_to_message_id: Optional[int] = None
        if isinstance(message_id_raw, (int, str)) and str(message_id_raw).strip():
            try:
                reply_to_message_id = int(message_id_raw)
            except ValueError:
                reply_to_message_id = None

        session_id = f"telegram:{chat_id}"
        state.append_session(session_id=session_id, role="user", content=user_message)
        chat_payload = {
            "source": "telegram",
            "event_type": "telegram_message",
            "session_id": session_id,
            "note": user_message,
            "task_description": "Telegram chat message",
            "task_status": "active",
            "task_done": False,
            "telegram_update_id": update_id,
            "telegram_chat_id": chat_id,
            "telegram_message_id": reply_to_message_id,
        }
        event_record = {
            "received_at": time.time(),
            "path": source_path,
            "session_id": session_id,
            "payload": chat_payload,
            "rendered_message": user_message,
            "forwarded": False,
            "telegram_sent": False,
        }

        assistant_text = _generate_chat_response(session_id=session_id, user_message=user_message)
        state.append_session(session_id=session_id, role="assistant", content=assistant_text)
        forward_result = _forward(
            message=assistant_text,
            payload={**chat_payload, "assistant_response": assistant_text},
        )
        _telegram_send_message(
            chat_id=chat_id,
            text=assistant_text,
            reply_to_message_id=reply_to_message_id,
        )
        event_record["forwarded"] = bool(forward_result.get("ok"))
        event_record["telegram_sent"] = True
        event_record["assistant_response"] = assistant_text
        state.record_event(event_record)
        return {
            "status": "ok",
            "session_id": session_id,
            "response": assistant_text,
            "forwarded": bool(forward_result.get("ok")),
            "telegram_sent": True,
        }

    def _run_telegram_polling() -> None:
        offset = 0
        with telegram_poll_lock:
            telegram_poll_state["started_at"] = time.time()
        while True:
            if not telegram_poll_enabled or not _telegram_token_ready():
                time.sleep(1.0)
                continue
            try:
                updates = _telegram_get_updates(offset=offset, timeout_s=telegram_poll_timeout_s)
                with telegram_poll_lock:
                    telegram_poll_state["last_polled_at"] = time.time()
                    telegram_poll_state["last_error"] = ""
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    update_id_raw = update.get("update_id")
                    update_id: Optional[int] = None
                    if isinstance(update_id_raw, (int, str)) and str(update_id_raw).strip():
                        try:
                            update_id = int(update_id_raw)
                        except ValueError:
                            update_id = None
                    if update_id is not None:
                        offset = max(offset, update_id + 1)
                        with telegram_poll_lock:
                            telegram_poll_state["last_update_id"] = update_id

                    message = update.get("message")
                    if not isinstance(message, dict):
                        continue
                    try:
                        _process_telegram_message(
                            message=message,
                            update_id=update_id,
                            source_path="/api/telegram/poll",
                        )
                    except Exception as exc:
                        state.record_event(
                            {
                                "received_at": time.time(),
                                "path": "/api/telegram/poll",
                                "error": str(exc),
                                "payload": {"update_id": update_id},
                            }
                        )
            except Exception as exc:
                with telegram_poll_lock:
                    telegram_poll_state["last_error"] = str(exc)
                time.sleep(float(telegram_poll_retry_s))

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
    .tabs { display: flex; gap: 8px; margin-bottom: 12px; }
    .tab-btn {
      border: 1px solid #2b3f7a;
      border-radius: 10px;
      padding: 8px 12px;
      background: #101935;
      color: #dbe4ff;
      cursor: pointer;
      font-size: 14px;
    }
    .tab-btn.active {
      background: #3552a6;
      border-color: #3e56a8;
      color: white;
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
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
    .settings-form {
      display: block;
      border: 1px solid #23315f;
      border-radius: 12px;
      background: #101935;
      padding: 12px;
    }
    .settings-row {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-bottom: 10px;
    }
    .settings-row label {
      width: 180px;
      font-size: 13px;
      color: #b3b9d6;
    }
    input[type="text"] {
      flex: 1;
      background: #0f1733;
      color: #e8ecff;
      border: 1px solid #2b3f7a;
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
    }
    input[type="password"] {
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
    <p class="muted">Chat via <code>/api/chat</code>. Session history is persisted locally by <code>session_id</code>.</p>
    <div id="status" class="status">Loading status...</div>
    <div class="tabs">
      <button id="tabChatBtn" class="tab-btn active" type="button">Chat</button>
      <button id="tabSettingsBtn" class="tab-btn" type="button">Settings</button>
    </div>

    <section id="tabChat" class="tab-panel active">
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
        Model API key (active): <code id="modelKey">(loading)</code><br>
        Forward URL: <code id="forwardUrl">(loading)</code><br>
        Forward API key: <code id="forwardKey">(loading)</code><br>
        If forwarding is enabled, each assistant response is also forwarded.
      </div>
    </section>

    <section id="tabSettings" class="tab-panel">
      <form id="settingsForm" class="settings-form">
        <div class="settings-row">
          <label for="openaiKeyInput">OpenAI API key</label>
          <input id="openaiKeyInput" type="password" placeholder="sk-..." />
        </div>
        <div class="settings-row">
          <label for="anthropicKeyInput">Anthropic API key</label>
          <input id="anthropicKeyInput" type="password" placeholder="sk-ant-..." />
        </div>
        <div class="settings-row">
          <label for="googleKeyInput">Google API key</label>
          <input id="googleKeyInput" type="password" placeholder="AIza..." />
        </div>
        <div class="settings-row">
          <label for="telegramTokenInput">Telegram bot token</label>
          <input id="telegramTokenInput" type="password" placeholder="123456:ABC-DEF..." />
        </div>
        <button type="submit">Save Settings</button>
      </form>
      <div class="meta">
        OpenAI key: <code id="openaiKeyMasked">(loading)</code><br>
        Anthropic key: <code id="anthropicKeyMasked">(loading)</code><br>
        Google key: <code id="googleKeyMasked">(loading)</code><br>
        Telegram bot token: <code id="telegramToken">(loading)</code><br>
        Telegram enabled: <code id="telegramEnabled">(loading)</code><br>
        Telegram mode: <code id="telegramMode">(loading)</code><br>
        Telegram polling active: <code id="telegramPollingActive">(loading)</code><br>
      </div>
    </section>
  </div>
  <script>
    const tabChatBtn = document.getElementById("tabChatBtn");
    const tabSettingsBtn = document.getElementById("tabSettingsBtn");
    const tabChat = document.getElementById("tabChat");
    const tabSettings = document.getElementById("tabSettings");

    function showTab(name) {
      const isChat = name === "chat";
      tabChat.classList.toggle("active", isChat);
      tabSettings.classList.toggle("active", !isChat);
      tabChatBtn.classList.toggle("active", isChat);
      tabSettingsBtn.classList.toggle("active", !isChat);
    }

    tabChatBtn.addEventListener("click", () => showTab("chat"));
    tabSettingsBtn.addEventListener("click", () => showTab("settings"));

    const chat = document.getElementById("chat");
    const statusEl = document.getElementById("status");
    const forwardUrlEl = document.getElementById("forwardUrl");
    const modelUrlEl = document.getElementById("modelUrl");
    const modelNameEl = document.getElementById("modelName");
    const modelKeyEl = document.getElementById("modelKey");
    const forwardKeyEl = document.getElementById("forwardKey");
    const telegramTokenEl = document.getElementById("telegramToken");
    const telegramEnabledEl = document.getElementById("telegramEnabled");
    const telegramModeEl = document.getElementById("telegramMode");
    const telegramPollingActiveEl = document.getElementById("telegramPollingActive");
    const openaiKeyMaskedEl = document.getElementById("openaiKeyMasked");
    const anthropicKeyMaskedEl = document.getElementById("anthropicKeyMasked");
    const googleKeyMaskedEl = document.getElementById("googleKeyMasked");
    const form = document.getElementById("chatForm");
    const settingsForm = document.getElementById("settingsForm");
    const openaiKeyInput = document.getElementById("openaiKeyInput");
    const anthropicKeyInput = document.getElementById("anthropicKeyInput");
    const googleKeyInput = document.getElementById("googleKeyInput");
    const telegramTokenInput = document.getElementById("telegramTokenInput");
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
        telegramTokenEl.textContent = data.telegram_bot_token || "(not set)";
        telegramEnabledEl.textContent = data.telegram_enabled ? "true" : "false";
        telegramModeEl.textContent = data.telegram_mode || "(not set)";
        telegramPollingActiveEl.textContent = data.telegram_polling_active ? "true" : "false";
        openaiKeyMaskedEl.textContent = data.openai_api_key || "(not set)";
        anthropicKeyMaskedEl.textContent = data.anthropic_api_key || "(not set)";
        googleKeyMaskedEl.textContent = data.google_api_key || "(not set)";
      } catch (err) {
        statusEl.textContent = "Health check failed: " + err;
        modelUrlEl.textContent = "(health failed)";
        modelNameEl.textContent = "(health failed)";
        modelKeyEl.textContent = "(health failed)";
        forwardUrlEl.textContent = "(health failed)";
        forwardKeyEl.textContent = "(health failed)";
        telegramTokenEl.textContent = "(health failed)";
        telegramEnabledEl.textContent = "(health failed)";
        telegramModeEl.textContent = "(health failed)";
        telegramPollingActiveEl.textContent = "(health failed)";
        openaiKeyMaskedEl.textContent = "(health failed)";
        anthropicKeyMaskedEl.textContent = "(health failed)";
        googleKeyMaskedEl.textContent = "(health failed)";
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

    settingsForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {
        openai_api_key: (openaiKeyInput.value || "").trim(),
        anthropic_api_key: (anthropicKeyInput.value || "").trim(),
        google_api_key: (googleKeyInput.value || "").trim(),
        telegram_bot_token: (telegramTokenInput.value || "").trim(),
      };
      try {
        const resp = await fetch("/api/config/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (resp.ok) {
          addMessage("Settings updated successfully.", "system");
          openaiKeyInput.value = "";
          anthropicKeyInput.value = "";
          googleKeyInput.value = "";
          telegramTokenInput.value = "";
          await loadHealth();
        } else {
          addMessage("Settings error: " + JSON.stringify(data, null, 2), "error");
        }
      } catch (err) {
        addMessage("Settings request error: " + err, "error");
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
        with telegram_poll_lock:
            poll_snapshot = dict(telegram_poll_state)
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
                "openai_api_key": _mask_secret(openai_api_key),
                "anthropic_api_key": _mask_secret(anthropic_api_key),
                "google_api_key": _mask_secret(google_api_key),
                "session_db_path": session_db_path,
                "shell_enabled": shell_enabled,
                "shell_cwd": shell_resolved_cwd,
                "shell_timeout_s": shell_timeout_s,
                "shell_max_calls_per_turn": shell_max_calls_per_turn,
                "telegram_enabled": _telegram_token_ready(),
                "telegram_bot_token": _mask_secret(telegram_bot_token),
                "telegram_mode": "long-polling",
                "telegram_poll_enabled": telegram_poll_enabled,
                "telegram_poll_timeout_s": telegram_poll_timeout_s,
                "telegram_polling_active": bool(telegram_poll_enabled and _telegram_token_ready()),
                "telegram_poll_state": poll_snapshot,
                "sessions": state.snapshot().get("sessions"),
            }
        )

    @app.route("/api/events", methods=["GET"])
    def events():
        return jsonify({"status": "ok", **state.snapshot()})

    @app.route("/hooks/videomemory-alert", methods=["POST"])
    def videomemory_hook():
        expected = gateway_token
        if expected:
            auth_header = request.headers.get("Authorization", "")
            expected_header = f"Bearer {expected}"
            if auth_header != expected_header:
                return jsonify({"status": "error", "error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        note = str(payload.get("note", "")).strip()
        io_id = str(payload.get("io_id", "")).strip() or "unknown"
        task_id = str(payload.get("task_id", "")).strip() or "unknown"
        task_desc = str(payload.get("task_description", "")).strip()
        session_id = f"videomemory:{io_id}"

        rendered = f"VideoMemory alert on {io_id} task {task_id}: {note or '(empty note)'}"
        if task_desc:
            rendered = f"{rendered}\nTask: {task_desc}"

        state.append_session(session_id=session_id, role="user", content=rendered)
        state.record_event(
            {
                "received_at": time.time(),
                "path": "/hooks/videomemory-alert",
                "session_id": session_id,
                "payload": payload,
                "rendered_message": rendered,
                "forwarded": False,
            }
        )
        return jsonify({"status": "ok", "session_id": session_id, "recorded": True})

    @app.route("/api/sessions/<session_id>", methods=["GET"])
    def get_session(session_id: str):
        session_id = session_id.strip()
        if not session_id:
            return jsonify({"status": "error", "error": "session_id is required"}), 400
        return jsonify({"status": "ok", "session_id": session_id, "history": state.get_session(session_id)})

    @app.route("/api/sessions", methods=["GET"])
    def list_sessions():
        return jsonify({"status": "ok", "sessions": state.list_sessions()})

    @app.route("/api/config/telegram", methods=["POST"])
    def update_telegram_config():
        nonlocal telegram_bot_token
        data = request.get_json(silent=True) or {}
        token = str(data.get("telegram_bot_token", "")).strip()

        telegram_bot_token = token
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        _upsert_env_var(".env", "TELEGRAM_BOT_TOKEN", token)

        return jsonify(
            {
                "status": "ok",
                "telegram_enabled": _telegram_token_ready(),
                "telegram_bot_token": _mask_secret(telegram_bot_token),
                "message": "TELEGRAM_BOT_TOKEN updated",
            }
        )

    @app.route("/api/config/settings", methods=["POST"])
    def update_settings():
        nonlocal telegram_bot_token, openai_api_key, anthropic_api_key, google_api_key
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        openai_api_key = str(data.get("openai_api_key", "")).strip()
        anthropic_api_key = str(data.get("anthropic_api_key", "")).strip()
        google_api_key = str(data.get("google_api_key", "")).strip()
        telegram_bot_token = str(data.get("telegram_bot_token", "")).strip()

        os.environ["OPENAI_API_KEY"] = openai_api_key
        os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key
        os.environ["GOOGLE_API_KEY"] = google_api_key
        os.environ["TELEGRAM_BOT_TOKEN"] = telegram_bot_token

        _upsert_env_var(".env", "OPENAI_API_KEY", openai_api_key)
        _upsert_env_var(".env", "ANTHROPIC_API_KEY", anthropic_api_key)
        _upsert_env_var(".env", "GOOGLE_API_KEY", google_api_key)
        _upsert_env_var(".env", "TELEGRAM_BOT_TOKEN", telegram_bot_token)

        return jsonify(
            {
                "status": "ok",
                "openai_api_key": _mask_secret(openai_api_key),
                "anthropic_api_key": _mask_secret(anthropic_api_key),
                "google_api_key": _mask_secret(google_api_key),
                "telegram_enabled": _telegram_token_ready(),
                "telegram_bot_token": _mask_secret(telegram_bot_token),
                "message": "Settings updated",
            }
        )

    @app.route("/api/telegram/webhook", methods=["POST"])
    def telegram_webhook():
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "Webhook mode is disabled. Telegram integration runs in long polling mode.",
                }
            ),
            410,
        )

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

    if telegram_poll_enabled:
        telegram_thread = threading.Thread(
            target=_run_telegram_polling,
            name="telegram-poller",
            daemon=True,
        )
        telegram_thread.start()

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "18789"))
    app.run(host=host, port=port, debug=False, threaded=True)
