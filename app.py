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
from typing import Any, Dict, List, Optional, Tuple

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

    def delete_session(self, session_id: str) -> int:
        return self.session_store.delete_session(session_id=session_id)

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

    def delete_session(self, session_id: str) -> int:
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM session_messages WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            return int(cur.rowcount or 0)

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


class DemoMcpServer:
    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "echo",
                "description": "Echoes text back to the caller.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            },
            {
                "name": "time_now",
                "description": "Returns current server time in seconds since epoch.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "echo":
            return {"ok": True, "text": str(arguments.get("text", ""))}
        if name == "time_now":
            return {"ok": True, "ts": time.time()}
        raise RuntimeError(f"Unknown demo tool: {name}")

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "transport": "inproc-demo"}


class StdioMcpClient:
    def __init__(self, server_id: str, command: str, args: List[str], timeout_s: int):
        self.server_id = server_id
        self.command = command
        self.args = args
        self.timeout_s = timeout_s
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.initialized = False

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.initialized = False

    def _write_message(self, payload: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError(f"MCP server {self.server_id} is not running")
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.proc.stdin.write(header + body)
        self.proc.stdin.flush()

    def _read_message(self) -> Dict[str, Any]:
        if not self.proc or not self.proc.stdout:
            raise RuntimeError(f"MCP server {self.server_id} is not running")

        headers: Dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server {self.server_id} closed stdout")
            text = line.decode("ascii", errors="ignore").strip()
            if not text:
                break
            if ":" in text:
                k, v = text.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        length = int(headers.get("content-length", "0"))
        if length <= 0:
            raise RuntimeError(f"MCP server {self.server_id} returned invalid content-length")
        body = self.proc.stdout.read(length)
        if not body:
            raise RuntimeError(f"MCP server {self.server_id} returned empty response body")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"MCP server {self.server_id} returned invalid JSON-RPC payload")
        return data

    def _request(self, method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        self._start()
        self.request_id += 1
        req_id = self.request_id
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write_message(payload)
        started = time.time()
        while True:
            if time.time() - started > self.timeout_s:
                raise RuntimeError(f"MCP request timed out for {self.server_id}:{method}")
            msg = self._read_message()
            if msg.get("id") != req_id:
                continue
            if msg.get("error"):
                raise RuntimeError(f"MCP {self.server_id}:{method} error: {json.dumps(msg['error'])}")
            return msg

    def _ensure_initialized(self) -> None:
        if self.initialized:
            return
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "adminagent", "version": "0.1.0"},
            },
        )
        if "result" not in result:
            raise RuntimeError(f"MCP initialize failed for {self.server_id}")
        self._write_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True

    def list_tools(self) -> List[Dict[str, Any]]:
        with self.lock:
            self._ensure_initialized()
            result = self._request("tools/list", {})
            tools = (result.get("result") or {}).get("tools") or []
            return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self._ensure_initialized()
            result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
            return (result.get("result") or {}) if isinstance(result, dict) else {}

    def health(self) -> Dict[str, Any]:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return {"status": "ok", "transport": "stdio"}
            return {"status": "idle", "transport": "stdio"}


class McpBroker:
    def __init__(self, enabled: bool, timeout_s: int, servers_json: str):
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.servers: Dict[str, Any] = {}
        if self.enabled:
            self.servers["demo"] = DemoMcpServer()
            self._load_servers_from_json(servers_json)

    def _load_servers_from_json(self, raw: str) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        try:
            data = json.loads(text)
        except Exception:
            return
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            server_id = str(item.get("id", "")).strip()
            transport = str(item.get("transport", "stdio")).strip().lower()
            command = str(item.get("command", "")).strip()
            args = item.get("args") or []
            if not server_id or transport != "stdio" or not command:
                continue
            safe_args = [str(a) for a in args] if isinstance(args, list) else []
            self.servers[server_id] = StdioMcpClient(
                server_id=server_id,
                command=command,
                args=safe_args,
                timeout_s=self.timeout_s,
            )

    def list_tools(self) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        all_tools: List[Dict[str, Any]] = []
        for server_id, client in self.servers.items():
            try:
                tools = client.list_tools()
            except Exception as exc:
                all_tools.append(
                    {
                        "name": f"{server_id}.__error__",
                        "description": f"Tool listing failed: {exc}",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                )
                continue
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                tool_name = str(tool.get("name", "")).strip()
                if not tool_name:
                    continue
                all_tools.append(
                    {
                        "name": f"{server_id}.{tool_name}",
                        "description": str(tool.get("description", "")).strip(),
                        "inputSchema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
                    }
                )
        return all_tools

    def call_tool(self, full_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("MCP is disabled")
        name = str(full_name or "").strip()
        if "." not in name:
            raise RuntimeError("MCP tool name must be in 'server.tool' format")
        server_id, tool_name = name.split(".", 1)
        server = self.servers.get(server_id)
        if server is None:
            raise RuntimeError(f"MCP server '{server_id}' is not configured")
        return server.call_tool(tool_name, arguments or {})

    def health(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "servers": []}
        result: List[Dict[str, Any]] = []
        for server_id, client in self.servers.items():
            try:
                info = client.health()
            except Exception as exc:
                info = {"status": "error", "error": str(exc)}
            result.append({"id": server_id, **info})
        return {"enabled": True, "servers": result}


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
    default_model = os.getenv("ADMINAGENT_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
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
    mcp_enabled = _env_bool("ADMINAGENT_MCP_ENABLED", True)
    mcp_timeout_s = max(1, int(os.getenv("ADMINAGENT_MCP_TIMEOUT_S", "20")))
    mcp_servers_json = os.getenv("ADMINAGENT_MCP_SERVERS_JSON", "[]")
    mcp_disabled_tools_raw = os.getenv("ADMINAGENT_MCP_DISABLED_TOOLS", "").strip()
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_poll_enabled = _env_bool("TELEGRAM_POLL_ENABLED", True)
    telegram_poll_timeout_s = max(1, min(50, int(os.getenv("TELEGRAM_POLL_TIMEOUT_S", "25"))))
    telegram_poll_retry_s = max(1, int(os.getenv("TELEGRAM_POLL_RETRY_S", "5")))
    openai_api_key = (
        os.getenv("OPENAI_API_KEY", "").strip()
        or os.getenv("ADMINAGENT_LLM_API_KEY", "").strip()
    )
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    google_api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    gateway_token = os.getenv("GATEWAY_TOKEN", "").strip()
    model_catalog: Dict[str, List[Dict[str, str]]] = {
        "openai": [
            {"id": "gpt-4o-mini", "label": "GPT-4o mini"},
            {"id": "gpt-5.3-codex", "label": "GPT 5.3-codex"},
            {"id": "gpt-5-thinking-high", "label": "GPT-5-Thinking (High)"},
            {"id": "gpt-5-mini", "label": "GPT-5 mini"},
            {"id": "gpt-5.2-pro", "label": "GPT-5.2 Pro"},
        ],
        "anthropic": [
            {"id": "claude-opus-4-6", "label": "Opus 4.6"},
            {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
            {"id": "claude-haiku-4-5", "label": "Haiku 4.5"},
        ],
        "google": [
            {"id": "gemini-3.1-pro", "api_model": "gemini-pro-latest", "label": "Gemini 3.1 Pro"},
            {"id": "gemini-3-flash", "api_model": "gemini-flash-latest", "label": "Gemini 3 Flash"},
        ],
    }

    def _flatten_model_catalog() -> List[Dict[str, str]]:
        flat: List[Dict[str, str]] = []
        for provider, models in model_catalog.items():
            for model in models:
                model_id = str(model.get("id", "")).strip()
                if not model_id:
                    continue
                flat.append(
                    {
                        "provider": provider,
                        "id": model_id,
                        "api_model": str(model.get("api_model", "")).strip() or model_id,
                        "label": str(model.get("label", "")).strip() or model_id,
                    }
                )
        return flat

    flat_model_catalog = _flatten_model_catalog()
    # Runtime-configurable tool flags controlled from the UI.
    shell_enabled_runtime = shell_enabled
    mcp_enabled_runtime = mcp_enabled
    mcp_disabled_tools: set[str] = {
        t.strip()
        for t in mcp_disabled_tools_raw.split(",")
        if t.strip()
    }

    # Keep broker initialized so toggling MCP on/off from UI does not require restart.
    mcp_broker = McpBroker(enabled=True, timeout_s=mcp_timeout_s, servers_json=mcp_servers_json)

    def _get_all_mcp_tools() -> List[Dict[str, Any]]:
        try:
            return mcp_broker.list_tools()
        except Exception:
            return []

    def _get_active_mcp_tools() -> List[Dict[str, Any]]:
        if not mcp_enabled_runtime:
            return []
        return [
            tool
            for tool in _get_all_mcp_tools()
            if str(tool.get("name", "")).strip() not in mcp_disabled_tools
        ]

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
        mcp_tools = _get_active_mcp_tools()
        if shell_enabled_runtime:
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
        if mcp_tools:
            tool_lines = []
            for tool in mcp_tools[:40]:
                tool_name = str(tool.get("name", "")).strip()
                if not tool_name:
                    continue
                desc = str(tool.get("description", "")).strip()
                if desc:
                    tool_lines.append(f"- {tool_name}: {desc}")
                else:
                    tool_lines.append(f"- {tool_name}")
            if tool_lines:
                effective_system_prompt = (
                    f"{effective_system_prompt}\n\n"
                    "You can request MCP tools with exactly one line in this format:\n"
                    "<tool:mcp name=\"server.tool\">{\"arg\":\"value\"}</tool:mcp>\n"
                    "Available MCP tools:\n"
                    + "\n".join(tool_lines)
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
        match = re.search(r"<tool:shell>(.+?)</tool:shell>", text, flags=re.DOTALL)
        if not match:
            return None
        command = match.group(1).strip()
        return command or None

    def _extract_mcp_tool_call(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        match = re.search(
            r'<tool:mcp\s+name="([^"]+)">\s*(.*?)\s*</tool:mcp>',
            text,
            flags=re.DOTALL,
        )
        if not match:
            return None
        tool_name = match.group(1).strip()
        args_text = match.group(2).strip() or "{}"
        try:
            args_obj = json.loads(args_text)
        except Exception as exc:
            raise RuntimeError(f"Invalid MCP tool arguments JSON: {exc}") from exc
        if not isinstance(args_obj, dict):
            raise RuntimeError("MCP tool arguments must be a JSON object")
        return tool_name, args_obj

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

    def _resolve_model(selected_model: Optional[str]) -> str:
        model = str(selected_model or "").strip()
        return model or default_model

    def _resolve_provider_and_model(selected_model: Optional[str]) -> Tuple[str, str]:
        model = _resolve_model(selected_model)
        for entry in flat_model_catalog:
            if model == entry["id"]:
                return entry["provider"], entry["api_model"]
        if ":" in model:
            provider_hint, model_name = model.split(":", 1)
            provider = provider_hint.strip().lower()
            model_name = model_name.strip()
            if provider in {"openai", "anthropic", "google"} and model_name:
                return provider, model_name

        lower = model.lower()
        if lower.startswith("claude"):
            return "anthropic", model
        if lower.startswith("gemini"):
            return "google", model
        return "openai", model

    def _split_messages_for_provider(messages: List[Dict[str, str]]) -> Tuple[str, List[Dict[str, str]]]:
        system_parts: List[str] = []
        convo: List[Dict[str, str]] = []
        for message in messages:
            role = str(message.get("role", "")).strip()
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
            elif role in {"user", "assistant"}:
                convo.append({"role": role, "content": content})
        return "\n\n".join(system_parts).strip(), convo

    def _extract_openai_responses_text(payload: Dict[str, Any]) -> str:
        # Prefer the canonical flattened field when present.
        output_text = str(payload.get("output_text", "")).strip()
        if output_text:
            return output_text

        # Fallback: walk output[].content[] blocks.
        output = payload.get("output")
        if isinstance(output, list):
            parts: List[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = str(block.get("text", "")).strip()
                    if text:
                        parts.append(text)
            joined = "\n".join(parts).strip()
            if joined:
                return joined
        return ""

    def _call_openai_chat_completions(messages: List[Dict[str, str]], model_name: str) -> str:
        if not openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI models")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {openai_api_key}"}
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.2,
        }
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=llm_timeout_s,
        )
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()
            raise RuntimeError(f"OpenAI API error {resp.status_code}: {detail[:500] or 'request failed'}")
        data = resp.json() if resp.text else {}
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenAI response missing choices")
        assistant_text = str((choices[0].get("message") or {}).get("content", "")).strip()
        if not assistant_text:
            raise RuntimeError("OpenAI response did not include assistant content")
        return assistant_text

    def _call_openai_responses(messages: List[Dict[str, str]], model_name: str) -> str:
        if not openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI models")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {openai_api_key}"}
        payload = {
            "model": model_name,
            "input": messages,
        }
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            json=payload,
            headers=headers,
            timeout=llm_timeout_s,
        )
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()
            raise RuntimeError(f"OpenAI API error {resp.status_code}: {detail[:500] or 'request failed'}")
        data = resp.json() if resp.text else {}
        if not isinstance(data, dict):
            raise RuntimeError("OpenAI responses API returned invalid payload")
        assistant_text = _extract_openai_responses_text(data)
        if not assistant_text:
            raise RuntimeError("OpenAI responses API did not include assistant content")
        return assistant_text

    def _call_openai(messages: List[Dict[str, str]], model_name: str) -> str:
        lower = model_name.lower()
        should_try_responses_first = lower.startswith("gpt-5") or "codex" in lower or lower.startswith("o1") or lower.startswith("o3")

        if should_try_responses_first:
            try:
                return _call_openai_responses(messages=messages, model_name=model_name)
            except Exception as exc:
                msg = str(exc).lower()
                if "404" in msg or "not found" in msg or "responses" in msg:
                    return _call_openai_chat_completions(messages=messages, model_name=model_name)
                raise
        return _call_openai_chat_completions(messages=messages, model_name=model_name)

    def _call_anthropic(messages: List[Dict[str, str]], model_name: str) -> str:
        if not anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic models")
        system_prompt, convo = _split_messages_for_provider(messages)
        anthropic_messages = [
            {"role": msg["role"], "content": [{"type": "text", "text": msg["content"]}]}
            for msg in convo
        ]
        payload: Dict[str, Any] = {
            "model": model_name,
            "max_tokens": 1024,
            "messages": anthropic_messages,
        }
        if system_prompt:
            payload["system"] = system_prompt
        headers = {
            "Content-Type": "application/json",
            "x-api-key": anthropic_api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers=headers,
            timeout=llm_timeout_s,
        )
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()
            hint = ""
            if resp.status_code == 404:
                hint = " (model may be unavailable for this key)"
            raise RuntimeError(
                f"Anthropic API error {resp.status_code}{hint}: {detail[:500] or 'request failed'}"
            )
        data = resp.json() if resp.text else {}
        content_blocks = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content_blocks, list) or not content_blocks:
            raise RuntimeError("Anthropic response missing content")
        text_parts = [str(block.get("text", "")) for block in content_blocks if isinstance(block, dict)]
        assistant_text = "\n".join([part for part in text_parts if part]).strip()
        if not assistant_text:
            raise RuntimeError("Anthropic response did not include assistant content")
        return assistant_text

    def _call_google(messages: List[Dict[str, str]], model_name: str) -> str:
        if not google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for Google models")
        system_prompt, convo = _split_messages_for_provider(messages)
        contents: List[Dict[str, Any]] = []
        for msg in convo:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        payload: Dict[str, Any] = {"contents": contents}
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={google_api_key}"
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=llm_timeout_s)
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()
            raise RuntimeError(f"Google API error {resp.status_code}: {detail[:500] or 'request failed'}")
        data = resp.json() if resp.text else {}
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("Google response missing candidates")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        text_parts = [str(part.get("text", "")) for part in parts if isinstance(part, dict)]
        assistant_text = "\n".join([part for part in text_parts if part]).strip()
        if not assistant_text:
            raise RuntimeError("Google response did not include assistant content")
        return assistant_text

    def _call_llm(messages: List[Dict[str, str]], selected_model: Optional[str]) -> str:
        provider, model_name = _resolve_provider_and_model(selected_model)
        if provider == "anthropic":
            return _call_anthropic(messages=messages, model_name=model_name)
        if provider == "google":
            return _call_google(messages=messages, model_name=model_name)
        return _call_openai(messages=messages, model_name=model_name)

    def _generate_chat_response(session_id: str, user_message: str, selected_model: Optional[str]) -> str:
        messages = _build_llm_messages(session_id=session_id, user_message=user_message)
        for _ in range(shell_max_calls_per_turn + 1):
            assistant_text = _call_llm(messages=messages, selected_model=selected_model)
            try:
                mcp_call = _extract_mcp_tool_call(assistant_text)
            except Exception as exc:
                return str(exc)
            if mcp_call is not None:
                tool_name, args = mcp_call
                if not mcp_enabled_runtime:
                    return "MCP access is disabled by configuration."
                if tool_name in mcp_disabled_tools:
                    return f"MCP tool '{tool_name}' is disabled by configuration."
                mcp_result = mcp_broker.call_tool(full_name=tool_name, arguments=args)
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOOL_RESULT mcp\n"
                            f"{json.dumps({'tool': tool_name, 'result': mcp_result}, ensure_ascii=True)}\n"
                            "Now continue and answer the user directly."
                        ),
                    }
                )
                continue
            shell_command = _extract_shell_command(assistant_text)
            if not shell_command:
                return assistant_text
            if not shell_enabled_runtime:
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

        assistant_text = _generate_chat_response(
            session_id=session_id,
            user_message=user_message,
            selected_model=default_model,
        )
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
    .chat-layout {
      display: flex;
      gap: 12px;
    }
    .session-sidebar {
      width: 230px;
      border: 1px solid #23315f;
      border-radius: 12px;
      background: #101935;
      padding: 10px;
      height: 448px;
      display: flex;
      flex-direction: column;
    }
    .session-sidebar-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 13px;
      color: #c5cff8;
    }
    .session-sidebar-actions {
      display: flex;
      gap: 6px;
    }
    .icon-btn {
      width: 28px;
      min-width: 28px;
      height: 28px;
      border-radius: 999px;
      padding: 0;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      line-height: 1;
    }
    .session-list {
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .session-item {
      width: 100%;
      text-align: left;
      border: 1px solid #2b3f7a;
      background: #0f1733;
      color: #dce5ff;
      border-radius: 8px;
      padding: 8px;
      cursor: pointer;
      font-size: 12px;
    }
    .session-item-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .session-item-main {
      flex: 1;
      min-width: 0;
      text-align: left;
      border: 0;
      background: transparent;
      color: inherit;
      cursor: pointer;
      padding: 0;
    }
    .session-delete-btn {
      width: 22px;
      min-width: 22px;
      height: 22px;
      border-radius: 6px;
      padding: 0;
      border: 1px solid #7a2b3a;
      background: #3a1520;
      color: #ffc4cd;
      font-size: 12px;
      line-height: 1;
    }
    .session-item.active {
      border-color: #3e56a8;
      background: #1d2a52;
    }
    .session-item-id {
      display: block;
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 2px;
      word-break: break-all;
    }
    .session-item-meta {
      display: block;
      font-size: 11px;
      color: #a8b0d4;
    }
    .chat-main {
      flex: 1;
      min-width: 0;
    }
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
    .events-panel {
      border: 1px solid #23315f;
      border-radius: 12px;
      background: #101935;
      padding: 12px;
    }
    .tools-panel {
      border: 1px solid #23315f;
      border-radius: 12px;
      background: #101935;
      padding: 12px;
    }
    .tools-row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
      font-size: 13px;
      color: #cbd5ff;
    }
    .tools-list {
      margin-top: 10px;
      border: 1px solid #2b3f7a;
      border-radius: 8px;
      padding: 8px;
      max-height: 260px;
      overflow-y: auto;
      background: #0f1733;
    }
    .tool-item {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
      font-size: 12px;
      color: #dce5ff;
    }
    .events-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      color: #c5cff8;
      font-size: 13px;
    }
    .events-list {
      max-height: 430px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .event-item {
      border: 1px solid #2b3f7a;
      border-radius: 8px;
      background: #0f1733;
      padding: 8px;
      font-size: 12px;
      color: #dce5ff;
    }
    .event-item-head {
      color: #a8b0d4;
      font-size: 11px;
      margin-bottom: 4px;
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
    select {
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
    @media (max-width: 900px) {
      .chat-layout {
        flex-direction: column;
      }
      .session-sidebar {
        width: auto;
        height: 180px;
      }
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
      <button id="tabToolsBtn" class="tab-btn" type="button">Tools</button>
      <button id="tabEventsBtn" class="tab-btn" type="button">Events</button>
    </div>

    <section id="tabChat" class="tab-panel active">
      <div class="chat-layout">
        <aside class="session-sidebar">
          <div class="session-sidebar-head">
            <strong>Sessions</strong>
            <div class="session-sidebar-actions">
              <button id="newSessionBtn" type="button" class="icon-btn" aria-label="New session" title="New session">+</button>
              <button id="refreshSessionsBtn" type="button">Refresh</button>
            </div>
          </div>
          <div id="sessionList" class="session-list"></div>
        </aside>
        <div class="chat-main">
          <div id="chat" class="chat"></div>
          <form id="chatForm">
            <select id="modelSelect" aria-label="Model" style="max-width: 240px;"></select>
            <input id="msgInput" type="text" placeholder="Type a message to test your agent..." required />
            <button type="submit">Send</button>
          </form>
        </div>
      </div>
      <div class="meta">
        Endpoint used: <code>/api/chat</code><br>
        Default model: <code id="modelName">(loading)</code><br>
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

    <section id="tabTools" class="tab-panel">
      <div class="tools-panel">
        <div class="tools-row">
          <input id="shellEnabledToggle" type="checkbox" />
          <label for="shellEnabledToggle">Enable shell access</label>
        </div>
        <div class="tools-row">
          <input id="mcpEnabledToggle" type="checkbox" />
          <label for="mcpEnabledToggle">Enable MCP tools</label>
        </div>
        <div class="meta" style="margin-top: 6px;">MCP tool allowlist:</div>
        <div id="mcpToolList" class="tools-list"></div>
        <div style="margin-top: 10px;">
          <button id="saveToolsBtn" type="button">Save Tool Settings</button>
        </div>
      </div>
    </section>

    <section id="tabEvents" class="tab-panel">
      <div class="events-panel">
        <div class="events-head">
          <strong>Event Log</strong>
          <button id="refreshEventsBtn" type="button">Refresh</button>
        </div>
        <div id="eventsList" class="events-list"></div>
      </div>
    </section>
  </div>
  <script>
    const tabChatBtn = document.getElementById("tabChatBtn");
    const tabSettingsBtn = document.getElementById("tabSettingsBtn");
    const tabToolsBtn = document.getElementById("tabToolsBtn");
    const tabEventsBtn = document.getElementById("tabEventsBtn");
    const tabChat = document.getElementById("tabChat");
    const tabSettings = document.getElementById("tabSettings");
    const tabTools = document.getElementById("tabTools");
    const tabEvents = document.getElementById("tabEvents");

    function showTab(name) {
      tabChat.classList.toggle("active", name === "chat");
      tabSettings.classList.toggle("active", name === "settings");
      tabTools.classList.toggle("active", name === "tools");
      tabEvents.classList.toggle("active", name === "events");
      tabChatBtn.classList.toggle("active", name === "chat");
      tabSettingsBtn.classList.toggle("active", name === "settings");
      tabToolsBtn.classList.toggle("active", name === "tools");
      tabEventsBtn.classList.toggle("active", name === "events");
      if (name === "tools") {
        loadToolsConfig().catch((err) => addMessage("Tools load error: " + err, "error"));
      }
      if (name === "events") {
        loadEvents().catch((err) => addMessage("Events load error: " + err, "error"));
      }
    }

    tabChatBtn.addEventListener("click", () => showTab("chat"));
    tabSettingsBtn.addEventListener("click", () => showTab("settings"));
    tabToolsBtn.addEventListener("click", () => showTab("tools"));
    tabEventsBtn.addEventListener("click", () => showTab("events"));

    const chat = document.getElementById("chat");
    const statusEl = document.getElementById("status");
    const forwardUrlEl = document.getElementById("forwardUrl");
    const modelNameEl = document.getElementById("modelName");
    const modelSelect = document.getElementById("modelSelect");
    const forwardKeyEl = document.getElementById("forwardKey");
    const telegramTokenEl = document.getElementById("telegramToken");
    const telegramEnabledEl = document.getElementById("telegramEnabled");
    const telegramModeEl = document.getElementById("telegramMode");
    const telegramPollingActiveEl = document.getElementById("telegramPollingActive");
    const openaiKeyMaskedEl = document.getElementById("openaiKeyMasked");
    const anthropicKeyMaskedEl = document.getElementById("anthropicKeyMasked");
    const googleKeyMaskedEl = document.getElementById("googleKeyMasked");
    const shellEnabledToggle = document.getElementById("shellEnabledToggle");
    const mcpEnabledToggle = document.getElementById("mcpEnabledToggle");
    const mcpToolList = document.getElementById("mcpToolList");
    const saveToolsBtn = document.getElementById("saveToolsBtn");
    const eventsListEl = document.getElementById("eventsList");
    const refreshEventsBtn = document.getElementById("refreshEventsBtn");
    const sessionListEl = document.getElementById("sessionList");
    const newSessionBtn = document.getElementById("newSessionBtn");
    const refreshSessionsBtn = document.getElementById("refreshSessionsBtn");
    const form = document.getElementById("chatForm");
    const settingsForm = document.getElementById("settingsForm");
    const openaiKeyInput = document.getElementById("openaiKeyInput");
    const anthropicKeyInput = document.getElementById("anthropicKeyInput");
    const googleKeyInput = document.getElementById("googleKeyInput");
    const telegramTokenInput = document.getElementById("telegramTokenInput");
    const input = document.getElementById("msgInput");
    let selectedSessionId = "local-dev";
    let lastToolsConfig = { mcp_tools: [] };

    const MODEL_CATALOG = {
      openai: [
        { id: "gpt-5.3-codex", label: "GPT 5.3-codex", description: "Coding-focused GPT-5 model" },
        { id: "gpt-5-thinking-high", label: "GPT-5-Thinking (High)", description: "High-reasoning GPT-5 mode" },
        { id: "gpt-5-mini", label: "GPT-5 mini", description: "Fast and efficient GPT-5 option" },
        { id: "gpt-5.2-pro", label: "GPT-5.2 Pro", description: "High-capability GPT-5.2 model" },
      ],
      anthropic: [
        { id: "claude-opus-4-6", label: "Opus 4.6", description: "Most capable for ambitious work" },
        { id: "claude-sonnet-4-6", label: "Sonnet 4.6", description: "Balanced performance and speed" },
        { id: "claude-haiku-4-5", label: "Haiku 4.5", description: "Fastest for quick answers" },
      ],
      google: [
        { id: "gemini-3.1-pro", apiModel: "gemini-pro-latest", label: "Gemini 3.1 Pro", description: "Latest Gemini Pro alias" },
        { id: "gemini-3-flash", apiModel: "gemini-flash-latest", label: "Gemini 3 Flash", description: "Latest Gemini Flash alias" },
      ],
    };

    function providerLabel(p) {
      if (p === "anthropic") return "Anthropic";
      if (p === "google") return "Google";
      return "OpenAI";
    }

    function getConfiguredProvidersFromHealth(data) {
      const configured = [];
      if (String(data?.openai_api_key || "").trim()) configured.push("openai");
      if (String(data?.anthropic_api_key || "").trim()) configured.push("anthropic");
      if (String(data?.google_api_key || "").trim()) configured.push("google");
      return configured;
    }

    function renderModelOptions(preferredModel, healthData) {
      const configuredProviders = getConfiguredProvidersFromHealth(healthData);
      const providerPool = configuredProviders.length ? configuredProviders : ["openai"];
      modelSelect.innerHTML = "";

      const flatModels = [];
      for (const provider of providerPool) {
        const models = MODEL_CATALOG[provider] || [];
        for (const m of models) {
          flatModels.push({ provider, ...m });
        }
      }

      for (const m of flatModels) {
        const option = document.createElement("option");
        option.value = m.id;
        option.textContent = m.label + " (" + providerLabel(m.provider) + ")";
        option.title = m.description || "";
        modelSelect.appendChild(option);
      }

      if (!flatModels.length) {
        const option = document.createElement("option");
        option.value = preferredModel || "";
        option.textContent = preferredModel || "No models";
        modelSelect.appendChild(option);
      }

      const selectedModel = flatModels.some((m) => m.id === preferredModel)
        ? preferredModel
        : (flatModels[0]?.id || preferredModel || "");
      modelSelect.value = selectedModel;
    }

    function addMessage(text, cls) {
      const el = document.createElement("div");
      el.className = "msg " + cls;
      el.textContent = text;
      chat.appendChild(el);
      chat.scrollTop = chat.scrollHeight;
    }

    function renderSessionHistory(sessionId, history) {
      chat.innerHTML = "";
      if (!Array.isArray(history) || history.length === 0) {
        addMessage("No messages yet for " + sessionId + ".", "system");
        return;
      }
      for (const item of history) {
        const role = String((item || {}).role || "").trim();
        const content = String((item || {}).content || "");
        if (!content) continue;
        if (role === "user") {
          addMessage("You: " + content, "user");
        } else if (role === "assistant") {
          addMessage("Assistant (" + sessionId + "): " + content, "system");
        } else {
          addMessage(content, "system");
        }
      }
    }

    async function loadSessionHistory(sessionId) {
      const safeSessionId = encodeURIComponent(sessionId);
      const resp = await fetch("/api/sessions/" + safeSessionId);
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || "failed to load session");
      }
      renderSessionHistory(sessionId, data.history || []);
    }

    function formatUpdatedAt(ts) {
      if (typeof ts !== "number" || !Number.isFinite(ts)) return "";
      const d = new Date(ts * 1000);
      return d.toLocaleString();
    }

    function formatTs(ts) {
      if (typeof ts !== "number" || !Number.isFinite(ts)) return "(unknown time)";
      return new Date(ts * 1000).toLocaleString();
    }

    async function loadEvents() {
      const resp = await fetch("/api/events");
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || "failed to load events");
      }
      const events = Array.isArray(data.events) ? data.events : [];
      eventsListEl.innerHTML = "";
      if (events.length === 0) {
        const empty = document.createElement("div");
        empty.className = "event-item";
        empty.textContent = "No events yet.";
        eventsListEl.appendChild(empty);
        return;
      }
      for (const event of events.slice().reverse()) {
        const item = document.createElement("div");
        item.className = "event-item";

        const head = document.createElement("div");
        head.className = "event-item-head";
        const path = String((event || {}).path || "(no path)");
        const sessionId = String((event || {}).session_id || "");
        head.textContent = formatTs(Number((event || {}).received_at)) + " | " + path + (sessionId ? " | " + sessionId : "");

        const body = document.createElement("div");
        const payload = (event || {}).payload;
        const rendered = String((event || {}).rendered_message || "").trim();
        const eventError = String((event || {}).error || "").trim();
        if (eventError) {
          body.textContent = "Error: " + eventError + (payload && typeof payload === "object" ? "\\n" + JSON.stringify(payload) : "");
        } else if (rendered) {
          body.textContent = rendered;
        } else if (payload && typeof payload === "object") {
          body.textContent = JSON.stringify(payload);
        } else {
          body.textContent = JSON.stringify(event);
        }

        item.appendChild(head);
        item.appendChild(body);
        eventsListEl.appendChild(item);
      }
    }

    function renderMcpToolList(mcpTools) {
      mcpToolList.innerHTML = "";
      const tools = Array.isArray(mcpTools) ? mcpTools : [];
      if (!tools.length) {
        const empty = document.createElement("div");
        empty.className = "tool-item";
        empty.textContent = "No MCP tools detected.";
        mcpToolList.appendChild(empty);
        return;
      }
      for (const tool of tools) {
        const name = String((tool || {}).name || "").trim();
        if (!name) continue;
        const row = document.createElement("div");
        row.className = "tool-item";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = !!tool.enabled;
        cb.dataset.toolName = name;
        const label = document.createElement("label");
        label.textContent = name;
        label.title = String((tool || {}).description || "");
        row.appendChild(cb);
        row.appendChild(label);
        mcpToolList.appendChild(row);
      }
    }

    async function loadToolsConfig() {
      const resp = await fetch("/api/config/tools");
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || "failed to load tools config");
      }
      lastToolsConfig = data;
      shellEnabledToggle.checked = !!data.shell_enabled;
      mcpEnabledToggle.checked = !!data.mcp_enabled;
      renderMcpToolList(data.mcp_tools || []);
    }

    async function loadSessions(preferredSessionId) {
      const targetSessionId = (preferredSessionId || selectedSessionId || "local-dev").trim();
      const resp = await fetch("/api/sessions");
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || "failed to load sessions");
      }
      const sessions = Array.isArray(data.sessions) ? data.sessions : [];
      sessionListEl.innerHTML = "";
      if (sessions.length === 0) {
        const empty = document.createElement("div");
        empty.className = "session-item-meta";
        empty.textContent = "No sessions yet.";
        sessionListEl.appendChild(empty);
        return;
      }

      for (const session of sessions) {
        const id = String(session.session_id || "").trim();
        if (!id) continue;
        const item = document.createElement("button");
        item.className = "session-item" + (id === targetSessionId ? " active" : "");
        const row = document.createElement("div");
        row.className = "session-item-row";

        const main = document.createElement("button");
        main.type = "button";
        main.className = "session-item-main";
        main.addEventListener("click", async () => {
          selectedSessionId = id;
          await loadSessionHistory(id);
          await loadSessions(id);
        });

        const idEl = document.createElement("span");
        idEl.className = "session-item-id";
        idEl.textContent = id;

        const metaEl = document.createElement("span");
        metaEl.className = "session-item-meta";
        const count = Number(session.message_count || 0);
        const updated = formatUpdatedAt(Number(session.updated_at));
        metaEl.textContent = count + " msgs" + (updated ? " | " + updated : "");

        main.appendChild(idEl);
        main.appendChild(metaEl);

        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "session-delete-btn";
        delBtn.title = "Delete session";
        delBtn.setAttribute("aria-label", "Delete session " + id);
        delBtn.textContent = "🗑";
        delBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm("Delete session '" + id + "'?")) return;
          try {
            const resp = await fetch("/api/sessions/" + encodeURIComponent(id), { method: "DELETE" });
            const data = await resp.json();
            if (!resp.ok) {
              throw new Error(data.error || "failed to delete session");
            }
            if (selectedSessionId === id) {
              selectedSessionId = "local-dev";
              chat.innerHTML = "";
              addMessage("Deleted session: " + id, "system");
            }
            await loadSessions(selectedSessionId);
            if (selectedSessionId && selectedSessionId !== id) {
              await loadSessionHistory(selectedSessionId);
            }
          } catch (err) {
            addMessage("Session delete error: " + err, "error");
          }
        });

        row.appendChild(main);
        row.appendChild(delBtn);
        item.appendChild(row);
        sessionListEl.appendChild(item);
      }
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
        modelNameEl.textContent = data.default_model || data.model || "(not set)";
        renderModelOptions(data.default_model || data.model || "", data);
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
        modelNameEl.textContent = "(health failed)";
        modelSelect.innerHTML = "";
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
      const sessionId = selectedSessionId || "local-dev";
      selectedSessionId = sessionId;
      if (!text) return;

      addMessage("You: " + text, "user");
      input.value = "";

      try {
        const resp = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, message: text, model: modelSelect.value || "" })
        });
        const data = await resp.json();
        if (resp.ok) {
          addMessage("Assistant (" + sessionId + ", " + (data.model || modelSelect.value || "model") + "): " + data.response, "system");
          await loadSessions(sessionId);
        } else {
          addMessage("Agent error: " + JSON.stringify(data, null, 2), "error");
        }
      } catch (err) {
        addMessage("Request error: " + err, "error");
      }
    });

    refreshSessionsBtn.addEventListener("click", async () => {
      try {
        await loadSessions(selectedSessionId);
      } catch (err) {
        addMessage("Session refresh error: " + err, "error");
      }
    });

    newSessionBtn.addEventListener("click", async () => {
      const newId = "session-" + Date.now();
      selectedSessionId = newId;
      chat.innerHTML = "";
      addMessage("Started new session: " + newId, "system");
      await loadSessions(newId);
    });

    refreshEventsBtn.addEventListener("click", async () => {
      try {
        await loadEvents();
      } catch (err) {
        addMessage("Events refresh error: " + err, "error");
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

    saveToolsBtn.addEventListener("click", async () => {
      const toolMap = {};
      const boxes = mcpToolList.querySelectorAll("input[type='checkbox'][data-tool-name]");
      for (const box of boxes) {
        toolMap[box.dataset.toolName] = box.checked;
      }
      const payload = {
        shell_enabled: !!shellEnabledToggle.checked,
        mcp_enabled: !!mcpEnabledToggle.checked,
        mcp_tools: toolMap,
      };
      try {
        const resp = await fetch("/api/config/tools", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) {
          addMessage("Tools settings error: " + JSON.stringify(data, null, 2), "error");
          return;
        }
        lastToolsConfig = data;
        addMessage("Tool settings updated.", "system");
        shellEnabledToggle.checked = !!data.shell_enabled;
        mcpEnabledToggle.checked = !!data.mcp_enabled;
        renderMcpToolList(data.mcp_tools || []);
        await loadHealth();
      } catch (err) {
        addMessage("Tools settings request error: " + err, "error");
      }
    });

    loadHealth();
    loadToolsConfig().catch((err) => addMessage("Tools bootstrap error: " + err, "error"));
    loadSessions(selectedSessionId)
      .then(() => loadSessionHistory(selectedSessionId))
      .catch((err) => addMessage("Session bootstrap error: " + err, "error"));
    loadEvents().catch((err) => addMessage("Events bootstrap error: " + err, "error"));
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
                "model": default_model,
                "default_model": default_model,
                "available_models": [entry["id"] for entry in flat_model_catalog],
                "openai_api_key": _mask_secret(openai_api_key),
                "anthropic_api_key": _mask_secret(anthropic_api_key),
                "google_api_key": _mask_secret(google_api_key),
                "session_db_path": session_db_path,
                "shell_enabled": shell_enabled_runtime,
                "shell_cwd": shell_resolved_cwd,
                "shell_timeout_s": shell_timeout_s,
                "shell_max_calls_per_turn": shell_max_calls_per_turn,
                "mcp": {
                    **mcp_broker.health(),
                    "enabled": mcp_enabled_runtime,
                    "disabled_tools": sorted(mcp_disabled_tools),
                    "tools": _get_all_mcp_tools(),
                },
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

    @app.route("/api/mcp/tools", methods=["GET"])
    def mcp_tools():
        tools = _get_all_mcp_tools()
        tools_with_flags = []
        for tool in tools:
            name = str(tool.get("name", "")).strip()
            tools_with_flags.append({**tool, "enabled": bool(name and name not in mcp_disabled_tools)})
        return jsonify({"status": "ok", "enabled": mcp_enabled_runtime, "tools": tools_with_flags})

    @app.route("/api/mcp/call", methods=["POST"])
    def mcp_call():
        data = request.get_json(silent=True) or {}
        tool = str(data.get("tool", "")).strip()
        arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
        if not tool:
            return jsonify({"status": "error", "error": "tool is required"}), 400
        if not mcp_enabled_runtime:
            return jsonify({"status": "error", "error": "MCP is disabled"}), 400
        if tool in mcp_disabled_tools:
            return jsonify({"status": "error", "error": f"MCP tool '{tool}' is disabled"}), 400
        try:
            result = mcp_broker.call_tool(full_name=tool, arguments=arguments)
            return jsonify({"status": "ok", "tool": tool, "result": result})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "tool": tool}), 502

    @app.route("/api/config/tools", methods=["GET"])
    def get_tools_config():
        tools = _get_all_mcp_tools()
        return jsonify(
            {
                "status": "ok",
                "shell_enabled": shell_enabled_runtime,
                "mcp_enabled": mcp_enabled_runtime,
                "mcp_tools": [
                    {
                        "name": str(tool.get("name", "")).strip(),
                        "description": str(tool.get("description", "")).strip(),
                        "enabled": str(tool.get("name", "")).strip() not in mcp_disabled_tools,
                    }
                    for tool in tools
                    if str(tool.get("name", "")).strip()
                ],
            }
        )

    @app.route("/api/config/tools", methods=["POST"])
    def update_tools_config():
        nonlocal shell_enabled_runtime, mcp_enabled_runtime, mcp_disabled_tools
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        if "shell_enabled" in data:
            shell_enabled_runtime = bool(data.get("shell_enabled"))
            os.environ["ADMINAGENT_SHELL_ENABLED"] = "1" if shell_enabled_runtime else "0"
            _upsert_env_var(".env", "ADMINAGENT_SHELL_ENABLED", "1" if shell_enabled_runtime else "0")

        if "mcp_enabled" in data:
            mcp_enabled_runtime = bool(data.get("mcp_enabled"))
            os.environ["ADMINAGENT_MCP_ENABLED"] = "1" if mcp_enabled_runtime else "0"
            _upsert_env_var(".env", "ADMINAGENT_MCP_ENABLED", "1" if mcp_enabled_runtime else "0")

        if "mcp_tools" in data:
            tool_map = data.get("mcp_tools")
            all_names = {
                str(tool.get("name", "")).strip()
                for tool in _get_all_mcp_tools()
                if str(tool.get("name", "")).strip()
            }
            next_disabled: set[str] = set()
            if isinstance(tool_map, dict):
                for name in all_names:
                    if name in tool_map and not bool(tool_map.get(name)):
                        next_disabled.add(name)
            mcp_disabled_tools = next_disabled
            disabled_csv = ",".join(sorted(mcp_disabled_tools))
            os.environ["ADMINAGENT_MCP_DISABLED_TOOLS"] = disabled_csv
            _upsert_env_var(".env", "ADMINAGENT_MCP_DISABLED_TOOLS", disabled_csv)

        return get_tools_config()

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

    @app.route("/api/sessions/<session_id>", methods=["DELETE"])
    def delete_session(session_id: str):
        session_id = session_id.strip()
        if not session_id:
            return jsonify({"status": "error", "error": "session_id is required"}), 400
        deleted = state.delete_session(session_id=session_id)
        return jsonify({"status": "ok", "session_id": session_id, "deleted_messages": deleted})

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

        if "openai_api_key" in data:
            next_openai_api_key = str(data.get("openai_api_key", "")).strip()
            if next_openai_api_key:
                openai_api_key = next_openai_api_key
        if "anthropic_api_key" in data:
            next_anthropic_api_key = str(data.get("anthropic_api_key", "")).strip()
            if next_anthropic_api_key:
                anthropic_api_key = next_anthropic_api_key
        if "google_api_key" in data:
            next_google_api_key = str(data.get("google_api_key", "")).strip()
            if next_google_api_key:
                google_api_key = next_google_api_key
        if "telegram_bot_token" in data:
            next_telegram_bot_token = str(data.get("telegram_bot_token", "")).strip()
            if next_telegram_bot_token:
                telegram_bot_token = next_telegram_bot_token

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
        selected_model = str(data.get("model", "")).strip() or default_model
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
            "model": selected_model,
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
            assistant_text = _generate_chat_response(
                session_id=session_id,
                user_message=user_message,
                selected_model=selected_model,
            )
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
                    "model": selected_model,
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
