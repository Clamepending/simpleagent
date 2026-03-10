#!/usr/bin/env python3
"""Multi-tenant local-first AI chat service with BOT_ID isolation."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import queue
import re
import select
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from html import unescape
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from flask import Flask, Response, g, jsonify, request, stream_with_context
from simpleagent.core.contract import (
    ContractValidationError,
    RUNTIME_CONTRACT_VERSION_V1,
    capabilities_payload,
    health_payload,
    parse_turn_request,
    turn_error_payload,
    turn_success_payload,
)
from simpleagent.core.tools import (
    ToolInvocation,
    ToolResultEnvelope,
    parse_tool_tag_v1,
)
from simpleagent.core.turn_engine import run_turn_loop


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str_with_legacy(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    legacy_name = name.replace("SIMPLEAGENT_", "ADMINAGENT_", 1)
    legacy_value = os.getenv(legacy_name)
    if legacy_value is not None:
        return legacy_value
    return default


def _env_bool_with_legacy(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    legacy_name = name.replace("SIMPLEAGENT_", "ADMINAGENT_", 1)
    legacy_value = os.getenv(legacy_name)
    if legacy_value is not None:
        return legacy_value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _env_int_with_legacy(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is not None:
        return int(value)
    legacy_name = name.replace("SIMPLEAGENT_", "ADMINAGENT_", 1)
    legacy_value = os.getenv(legacy_name)
    if legacy_value is not None:
        return int(legacy_value)
    return int(default)


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


def _safe_bot_id(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(raw or "").strip()).strip("-")
    return cleaned[:64]


class AppState:
    def __init__(self, session_store: "SqliteSessionStore"):
        self.lock = threading.Lock()
        self.events: List[Dict[str, Any]] = []
        self.last_forward: Optional[Dict[str, Any]] = None
        self.session_store = session_store

    def record_event(self, event: Dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)
            self.events = self.events[-500:]

    def set_last_forward(self, info: Dict[str, Any]) -> None:
        with self.lock:
            self.last_forward = info

    def append_session(self, bot_id: str, session_id: str, role: str, content: str) -> None:
        self.session_store.append_message(bot_id=bot_id, session_id=session_id, role=role, content=content)

    def get_session(self, bot_id: str, session_id: str) -> List[Dict[str, Any]]:
        return self.session_store.get_session(bot_id=bot_id, session_id=session_id)

    def list_sessions(self, bot_id: str) -> List[Dict[str, Any]]:
        return self.session_store.list_sessions(bot_id=bot_id)

    def delete_session(self, bot_id: str, session_id: str) -> int:
        return self.session_store.delete_session(bot_id=bot_id, session_id=session_id)

    def snapshot(self, bot_id: Optional[str] = None) -> Dict[str, Any]:
        sessions_snapshot = self.session_store.snapshot(bot_id=bot_id)
        with self.lock:
            events = list(self.events)
            if bot_id:
                events = [evt for evt in events if str(evt.get("bot_id", "")) == bot_id]
            return {
                "events": events,
                "last_forward": dict(self.last_forward) if self.last_forward else None,
                "sessions": sessions_snapshot,
            }


class SqliteSessionStore:
    def __init__(self, db_path: str, max_messages_per_session: int = 100):
        self.db_path = db_path.strip() or "simpleagent.db"
        self.max_messages_per_session = max(1, int(max_messages_per_session))
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

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
                    bot_id TEXT NOT NULL DEFAULT 'legacy-default',
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            self._ensure_column(conn, "session_messages", "bot_id", "bot_id TEXT NOT NULL DEFAULT 'legacy-default'")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_messages_bot_session_id_id
                ON session_messages(bot_id, session_id, id)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bots (
                    bot_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    telegram_bot_token TEXT NOT NULL DEFAULT '',
                    telegram_owner_user_id INTEGER,
                    telegram_owner_chat_id INTEGER,
                    heartbeat_interval_s INTEGER NOT NULL DEFAULT 300,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._ensure_column(conn, "bots", "telegram_bot_token", "telegram_bot_token TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "bots", "telegram_owner_user_id", "telegram_owner_user_id INTEGER")
            self._ensure_column(conn, "bots", "telegram_owner_chat_id", "telegram_owner_chat_id INTEGER")
            self._ensure_column(conn, "bots", "heartbeat_interval_s", "heartbeat_interval_s INTEGER NOT NULL DEFAULT 300")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_telegram_token_unique
                ON bots(telegram_bot_token)
                WHERE telegram_bot_token IS NOT NULL AND telegram_bot_token != ''
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbound_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    bot_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    locked_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_queue_status_available
                ON inbound_queue(status, available_at, id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    bot_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    result_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    locked_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbound_queue_status_available
                ON outbound_queue(status, available_at, id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_poll_offsets (
                    bot_id TEXT PRIMARY KEY,
                    next_offset INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _bot_from_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "bot_id": str(row["bot_id"]),
            "name": str(row["name"]),
            "model": str(row["model"]),
            "telegram_bot_token": str(row["telegram_bot_token"] or ""),
            "telegram_owner_user_id": int(row["telegram_owner_user_id"]) if row["telegram_owner_user_id"] is not None else None,
            "telegram_owner_chat_id": int(row["telegram_owner_chat_id"]) if row["telegram_owner_chat_id"] is not None else None,
            "heartbeat_interval_s": max(1, int(row["heartbeat_interval_s"] or 300)),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def create_bot(
        self,
        bot_id: str,
        name: str,
        model: str,
        telegram_bot_token: str = "",
        heartbeat_interval_s: int = 300,
    ) -> Dict[str, Any]:
        now = time.time()
        clean_id = _safe_bot_id(bot_id)
        if not clean_id:
            raise RuntimeError("bot_id is invalid")
        clean_name = str(name or clean_id).strip() or clean_id
        clean_model = str(model or "").strip()
        if not clean_model:
            raise RuntimeError("model is required")
        token = str(telegram_bot_token or "").strip()
        interval = max(1, int(heartbeat_interval_s))
        with self.lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO bots(
                        bot_id, name, model, telegram_bot_token, telegram_owner_user_id,
                        telegram_owner_chat_id, heartbeat_interval_s, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                    """,
                    (clean_id, clean_name, clean_model, token, interval, now, now),
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "telegram_bot_token" in msg:
                    raise RuntimeError("telegram_bot_token is already linked to another bot") from exc
                raise RuntimeError("bot_id already exists") from exc
            conn.commit()
            row = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if not row:
                raise RuntimeError("failed to create bot")
            return self._bot_from_row(row)

    def get_bot(self, bot_id: str) -> Optional[Dict[str, Any]]:
        clean_id = _safe_bot_id(bot_id)
        if not clean_id:
            return None
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if not row:
                return None
            return self._bot_from_row(row)

    def list_bots(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM bots
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._bot_from_row(row) for row in rows]

    def update_bot(
        self,
        bot_id: str,
        *,
        name: Optional[str] = None,
        model: Optional[str] = None,
        telegram_bot_token: Optional[str] = None,
        heartbeat_interval_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        clean_id = _safe_bot_id(bot_id)
        if not clean_id:
            raise RuntimeError("bot_id is required")

        sets: List[str] = []
        params: List[Any] = []
        if name is not None:
            clean_name = str(name).strip()
            if clean_name:
                sets.append("name = ?")
                params.append(clean_name)
        if model is not None:
            clean_model = str(model).strip()
            if clean_model:
                sets.append("model = ?")
                params.append(clean_model)
        if telegram_bot_token is not None:
            sets.append("telegram_bot_token = ?")
            params.append(str(telegram_bot_token).strip())
        if heartbeat_interval_s is not None:
            sets.append("heartbeat_interval_s = ?")
            params.append(max(1, int(heartbeat_interval_s)))

        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if not row:
                raise RuntimeError("bot not found")

            if sets:
                sets.append("updated_at = ?")
                params.append(time.time())
                params.append(clean_id)
                try:
                    conn.execute(f"UPDATE bots SET {', '.join(sets)} WHERE bot_id = ?", tuple(params))
                except sqlite3.IntegrityError as exc:
                    msg = str(exc).lower()
                    if "telegram_bot_token" in msg:
                        raise RuntimeError("telegram_bot_token is already linked to another bot") from exc
                    raise
                conn.commit()

            updated = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if not updated:
                raise RuntimeError("bot not found")
            return self._bot_from_row(updated)

    def set_telegram_owner(self, bot_id: str, user_id: int, chat_id: int) -> Dict[str, Any]:
        clean_id = _safe_bot_id(bot_id)
        if not clean_id:
            raise RuntimeError("bot_id is required")
        uid = int(user_id)
        cid = int(chat_id)
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if not row:
                raise RuntimeError("bot not found")
            existing_owner = row["telegram_owner_user_id"]
            if existing_owner is None:
                conn.execute(
                    """
                    UPDATE bots
                    SET telegram_owner_user_id = ?, telegram_owner_chat_id = ?, updated_at = ?
                    WHERE bot_id = ?
                    """,
                    (uid, cid, time.time(), clean_id),
                )
                conn.commit()
            elif int(existing_owner) != uid:
                raise RuntimeError("telegram owner mismatch")
            updated = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if not updated:
                raise RuntimeError("bot not found")
            return self._bot_from_row(updated)

    def append_message(self, bot_id: str, session_id: str, role: str, content: str) -> None:
        clean_bot_id = _safe_bot_id(bot_id)
        if not clean_bot_id:
            raise RuntimeError("bot_id is required")
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            raise RuntimeError("session_id is required")

        ts = time.time()
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO session_messages(bot_id, session_id, role, content, ts) VALUES (?, ?, ?, ?, ?)",
                (clean_bot_id, clean_session_id, role, content, ts),
            )
            conn.execute(
                """
                DELETE FROM session_messages
                WHERE bot_id = ?
                  AND session_id = ?
                  AND id NOT IN (
                      SELECT id
                      FROM session_messages
                      WHERE bot_id = ?
                        AND session_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (clean_bot_id, clean_session_id, clean_bot_id, clean_session_id, self.max_messages_per_session),
            )
            conn.commit()

    def get_session(self, bot_id: str, session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        clean_bot_id = _safe_bot_id(bot_id)
        clean_session_id = str(session_id or "").strip()
        if not clean_bot_id or not clean_session_id:
            return []
        max_rows = self.max_messages_per_session if limit is None else max(1, int(limit))
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, ts
                FROM (
                    SELECT id, role, content, ts
                    FROM session_messages
                    WHERE bot_id = ?
                      AND session_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (clean_bot_id, clean_session_id, max_rows),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"], "ts": row["ts"]} for row in rows]

    def list_sessions(self, bot_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        clean_bot_id = _safe_bot_id(bot_id)
        if not clean_bot_id:
            return []
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, COUNT(*) AS message_count, MAX(ts) AS updated_at
                FROM session_messages
                WHERE bot_id = ?
                GROUP BY session_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (clean_bot_id, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "message_count": int(row["message_count"]),
                "updated_at": float(row["updated_at"]) if row["updated_at"] is not None else None,
            }
            for row in rows
        ]

    def delete_session(self, bot_id: str, session_id: str) -> int:
        clean_bot_id = _safe_bot_id(bot_id)
        clean_session_id = str(session_id or "").strip()
        if not clean_bot_id or not clean_session_id:
            return 0
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM session_messages WHERE bot_id = ? AND session_id = ?",
                (clean_bot_id, clean_session_id),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def snapshot(self, bot_id: Optional[str] = None) -> Dict[str, Any]:
        with self.lock, self._connect() as conn:
            if bot_id:
                clean_bot_id = _safe_bot_id(bot_id)
                count_row = conn.execute(
                    "SELECT COUNT(DISTINCT session_id) AS session_count FROM session_messages WHERE bot_id = ?",
                    (clean_bot_id,),
                ).fetchone()
                id_rows = conn.execute(
                    "SELECT DISTINCT session_id FROM session_messages WHERE bot_id = ? ORDER BY session_id ASC",
                    (clean_bot_id,),
                ).fetchall()
                return {
                    "bot_id": clean_bot_id,
                    "session_count": int((count_row or {"session_count": 0})["session_count"]),
                    "session_ids": [row["session_id"] for row in id_rows],
                }

            count_row = conn.execute("SELECT COUNT(DISTINCT bot_id || ':' || session_id) AS session_count FROM session_messages").fetchone()
            bot_rows = conn.execute(
                """
                SELECT bot_id, COUNT(DISTINCT session_id) AS session_count
                FROM session_messages
                GROUP BY bot_id
                ORDER BY bot_id ASC
                """
            ).fetchall()
            return {
                "session_count": int((count_row or {"session_count": 0})["session_count"]),
                "bots": [
                    {
                        "bot_id": str(row["bot_id"]),
                        "session_count": int(row["session_count"]),
                    }
                    for row in bot_rows
                ],
            }

    def _queue_row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload_raw = str(row["payload_json"] or "")
        result_raw = str(row["result_json"] or "")
        payload: Dict[str, Any]
        result: Dict[str, Any]
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except Exception:
            payload = {}
        try:
            result = json.loads(result_raw) if result_raw else {}
        except Exception:
            result = {}
        return {
            "id": int(row["id"]),
            "event_id": str(row["event_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "bot_id": str(row["bot_id"]),
            "session_id": str(row["session_id"]) if "session_id" in row.keys() else "",
            "source": str(row["source"]) if "source" in row.keys() else "",
            "event_type": str(row["event_type"]) if "event_type" in row.keys() else "",
            "channel": str(row["channel"]) if "channel" in row.keys() else "",
            "payload": payload,
            "status": str(row["status"]),
            "result": result,
            "error": str(row["error"] or ""),
            "attempts": int(row["attempts"] or 0),
            "available_at": float(row["available_at"] or 0.0),
            "locked_at": float(row["locked_at"]) if row["locked_at"] is not None else None,
            "created_at": float(row["created_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def _compute_retry_delay_s(self, attempts: int, retry_base_s: int, retry_cap_s: int) -> int:
        base = max(1, int(retry_base_s))
        cap = max(base, int(retry_cap_s))
        tries = max(1, int(attempts))
        delay = base * (2 ** max(0, tries - 1))
        return int(min(cap, delay))

    def enqueue_inbound_event(
        self,
        *,
        bot_id: str,
        session_id: str,
        source: str,
        event_type: str,
        payload: Dict[str, Any],
        event_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_bot_id = _safe_bot_id(bot_id)
        clean_session_id = str(session_id or "").strip()
        clean_source = str(source or "").strip() or "unknown"
        clean_event_type = str(event_type or "").strip() or "unknown"
        payload_obj = payload if isinstance(payload, dict) else {}
        payload_text = json.dumps(payload_obj, ensure_ascii=True)
        now = time.time()
        eid = str(event_id or f"in_{uuid.uuid4().hex}").strip()
        idem = str(idempotency_key or eid).strip()
        if not clean_bot_id:
            raise RuntimeError("bot_id is required")
        if not clean_session_id:
            raise RuntimeError("session_id is required")

        with self.lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM inbound_queue WHERE idempotency_key = ?",
                (idem,),
            ).fetchone()
            if existing:
                return self._queue_row_to_dict(existing)
            conn.execute(
                """
                INSERT INTO inbound_queue(
                    event_id, idempotency_key, bot_id, session_id, source, event_type,
                    payload_json, status, result_json, error, attempts, available_at,
                    locked_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '', '', 0, ?, NULL, ?, ?)
                """,
                (
                    eid,
                    idem,
                    clean_bot_id,
                    clean_session_id,
                    clean_source,
                    clean_event_type,
                    payload_text,
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM inbound_queue WHERE event_id = ?", (eid,)).fetchone()
            if not row:
                raise RuntimeError("failed to enqueue inbound event")
            return self._queue_row_to_dict(row)

    def claim_next_inbound_event(self, worker_id: str, lock_timeout_s: int = 120) -> Optional[Dict[str, Any]]:
        now = time.time()
        worker_hint = str(worker_id or "worker").strip()[:48]
        stale_before = now - max(1, int(lock_timeout_s))
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbound_queue
                SET status = 'queued',
                    locked_at = NULL,
                    available_at = ?,
                    updated_at = ?
                WHERE status = 'processing' AND locked_at IS NOT NULL AND locked_at <= ?
                """,
                (now, now, stale_before),
            )
            row = conn.execute(
                """
                SELECT * FROM inbound_queue
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                """
                UPDATE inbound_queue
                SET status = 'processing',
                    attempts = attempts + 1,
                    locked_at = ?,
                    updated_at = ?,
                    error = ''
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, int(row["id"])),
            )
            if int(cur.rowcount or 0) != 1:
                conn.commit()
                return None
            conn.commit()
            claimed = conn.execute("SELECT * FROM inbound_queue WHERE id = ?", (int(row["id"]),)).fetchone()
            if not claimed:
                return None
            data = self._queue_row_to_dict(claimed)
            data["worker_id"] = worker_hint
            return data

    def get_inbound_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        eid = str(event_id or "").strip()
        if not eid:
            return None
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM inbound_queue WHERE event_id = ?", (eid,)).fetchone()
            if not row:
                return None
            return self._queue_row_to_dict(row)

    def complete_inbound_event(self, event_id: str, result: Dict[str, Any]) -> None:
        eid = str(event_id or "").strip()
        if not eid:
            return
        now = time.time()
        result_text = json.dumps(result if isinstance(result, dict) else {}, ensure_ascii=True)
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbound_queue
                SET status = 'done', result_json = ?, error = '', updated_at = ?
                WHERE event_id = ?
                """,
                (result_text, now, eid),
            )
            conn.commit()

    def fail_inbound_event(
        self,
        event_id: str,
        error: str,
        *,
        max_attempts: int = 1,
        retry_base_s: int = 1,
        retry_cap_s: int = 1,
    ) -> Dict[str, Any]:
        eid = str(event_id or "").strip()
        if not eid:
            return {}
        now = time.time()
        max_tries = max(1, int(max_attempts))
        clean_error = str(error or "unknown error")
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM inbound_queue WHERE event_id = ?", (eid,)).fetchone()
            if not row:
                return {}
            status = str(row["status"] or "").strip().lower()
            attempts = int(row["attempts"] or 0)

            # Retry policy is applied for worker-claimed events only.
            if status == "processing" and attempts < max_tries:
                delay_s = self._compute_retry_delay_s(attempts, retry_base_s, retry_cap_s)
                conn.execute(
                    """
                    UPDATE inbound_queue
                    SET status = 'queued',
                        error = ?,
                        available_at = ?,
                        locked_at = NULL,
                        updated_at = ?
                    WHERE event_id = ?
                    """,
                    (clean_error, now + float(delay_s), now, eid),
                )
            else:
                terminal = "dead_letter" if status == "processing" and attempts >= max_tries else "error"
                conn.execute(
                    """
                    UPDATE inbound_queue
                    SET status = ?,
                        error = ?,
                        locked_at = NULL,
                        updated_at = ?
                    WHERE event_id = ?
                    """,
                    (terminal, clean_error, now, eid),
                )
            conn.commit()
            updated = conn.execute("SELECT * FROM inbound_queue WHERE event_id = ?", (eid,)).fetchone()
            return self._queue_row_to_dict(updated) if updated else {}

    def queue_stats(self) -> Dict[str, Any]:
        with self.lock, self._connect() as conn:
            inbound = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM inbound_queue
                GROUP BY status
                """
            ).fetchall()
            outbound = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM outbound_queue
                GROUP BY status
                """
            ).fetchall()
        return {
            "inbound": {str(row["status"]): int(row["c"]) for row in inbound},
            "outbound": {str(row["status"]): int(row["c"]) for row in outbound},
        }

    def queue_metrics(self) -> Dict[str, Any]:
        now = time.time()
        with self.lock, self._connect() as conn:
            inbound_counts = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM inbound_queue
                GROUP BY status
                """
            ).fetchall()
            outbound_counts = conn.execute(
                """
                SELECT status, COUNT(*) AS c
                FROM outbound_queue
                GROUP BY status
                """
            ).fetchall()

            inbound_oldest_row = conn.execute(
                """
                SELECT MIN(created_at) AS ts
                FROM inbound_queue
                WHERE status = 'queued'
                """
            ).fetchone()
            outbound_oldest_row = conn.execute(
                """
                SELECT MIN(created_at) AS ts
                FROM outbound_queue
                WHERE status = 'queued'
                """
            ).fetchone()

            inbound_ready_row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM inbound_queue
                WHERE status = 'queued' AND available_at <= ?
                """,
                (now,),
            ).fetchone()
            outbound_ready_row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM outbound_queue
                WHERE status = 'queued' AND available_at <= ?
                """,
                (now,),
            ).fetchone()

            inbound_top_bots = conn.execute(
                """
                SELECT bot_id, COUNT(*) AS c
                FROM inbound_queue
                WHERE status = 'queued'
                GROUP BY bot_id
                ORDER BY c DESC, bot_id ASC
                LIMIT 10
                """
            ).fetchall()
            inbound_ready_top_bots = conn.execute(
                """
                SELECT bot_id, COUNT(*) AS c
                FROM inbound_queue
                WHERE status = 'queued' AND available_at <= ?
                GROUP BY bot_id
                ORDER BY c DESC, bot_id ASC
                LIMIT 10
                """,
                (now,),
            ).fetchall()
            outbound_top_bots = conn.execute(
                """
                SELECT bot_id, COUNT(*) AS c
                FROM outbound_queue
                WHERE status = 'queued'
                GROUP BY bot_id
                ORDER BY c DESC, bot_id ASC
                LIMIT 10
                """
            ).fetchall()
            outbound_ready_top_bots = conn.execute(
                """
                SELECT bot_id, COUNT(*) AS c
                FROM outbound_queue
                WHERE status = 'queued' AND available_at <= ?
                GROUP BY bot_id
                ORDER BY c DESC, bot_id ASC
                LIMIT 10
                """,
                (now,),
            ).fetchall()

        inbound_counts_map = {str(row["status"]): int(row["c"]) for row in inbound_counts}
        outbound_counts_map = {str(row["status"]): int(row["c"]) for row in outbound_counts}
        inbound_oldest_ts = float(inbound_oldest_row["ts"]) if inbound_oldest_row and inbound_oldest_row["ts"] is not None else None
        outbound_oldest_ts = float(outbound_oldest_row["ts"]) if outbound_oldest_row and outbound_oldest_row["ts"] is not None else None

        return {
            "now": now,
            "inbound": {
                "counts": inbound_counts_map,
                "queued_ready": int((inbound_ready_row or {"c": 0})["c"]),
                "oldest_queued_created_at": inbound_oldest_ts,
                "oldest_queued_age_s": (max(0.0, now - inbound_oldest_ts) if inbound_oldest_ts is not None else None),
                "queued_by_bot_top": [
                    {"bot_id": str(row["bot_id"]), "queued": int(row["c"])}
                    for row in inbound_top_bots
                ],
                "queued_ready_by_bot_top": [
                    {"bot_id": str(row["bot_id"]), "queued_ready": int(row["c"])}
                    for row in inbound_ready_top_bots
                ],
            },
            "outbound": {
                "counts": outbound_counts_map,
                "queued_ready": int((outbound_ready_row or {"c": 0})["c"]),
                "oldest_queued_created_at": outbound_oldest_ts,
                "oldest_queued_age_s": (max(0.0, now - outbound_oldest_ts) if outbound_oldest_ts is not None else None),
                "queued_by_bot_top": [
                    {"bot_id": str(row["bot_id"]), "queued": int(row["c"])}
                    for row in outbound_top_bots
                ],
                "queued_ready_by_bot_top": [
                    {"bot_id": str(row["bot_id"]), "queued_ready": int(row["c"])}
                    for row in outbound_ready_top_bots
                ],
            },
        }

    def engagement_metrics(
        self,
        *,
        window_days: int = 7,
        engaged_event_threshold: int = 3,
        limit: int = 50,
    ) -> Dict[str, Any]:
        clean_window_days = max(1, min(90, int(window_days)))
        clean_threshold = max(1, min(1000, int(engaged_event_threshold)))
        clean_limit = max(1, min(500, int(limit)))

        now = time.time()
        day_s = 24.0 * 60.0 * 60.0
        current_start = now - (float(clean_window_days) * day_s)
        previous_start = now - (float(clean_window_days * 2) * day_s)
        start_24h = now - day_s

        with self.lock, self._connect() as conn:
            active_24h_row = conn.execute(
                """
                SELECT COUNT(DISTINCT bot_id) AS c
                FROM (
                    SELECT bot_id FROM inbound_queue WHERE created_at >= ?
                    UNION
                    SELECT bot_id FROM outbound_queue WHERE created_at >= ?
                )
                """,
                (start_24h, start_24h),
            ).fetchone()

            active_current_row = conn.execute(
                """
                SELECT COUNT(DISTINCT bot_id) AS c
                FROM (
                    SELECT bot_id FROM inbound_queue WHERE created_at >= ?
                    UNION
                    SELECT bot_id FROM outbound_queue WHERE created_at >= ?
                )
                """,
                (current_start, current_start),
            ).fetchone()

            success_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count,
                    SUM(CASE WHEN status IN ('done', 'error', 'dead_letter') THEN 1 ELSE 0 END) AS terminal_count
                FROM (
                    SELECT status FROM inbound_queue WHERE created_at >= ?
                    UNION ALL
                    SELECT status FROM outbound_queue WHERE created_at >= ?
                )
                """,
                (current_start, current_start),
            ).fetchone()

            previous_active_row = conn.execute(
                """
                SELECT COUNT(DISTINCT bot_id) AS c
                FROM (
                    SELECT bot_id FROM inbound_queue WHERE created_at >= ? AND created_at < ?
                    UNION
                    SELECT bot_id FROM outbound_queue WHERE created_at >= ? AND created_at < ?
                )
                """,
                (previous_start, current_start, previous_start, current_start),
            ).fetchone()

            retained_row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM (
                    SELECT DISTINCT bot_id
                    FROM (
                        SELECT bot_id, created_at FROM inbound_queue
                        UNION ALL
                        SELECT bot_id, created_at FROM outbound_queue
                    )
                    WHERE created_at >= ? AND created_at < ?
                ) previous_active
                INNER JOIN (
                    SELECT DISTINCT bot_id
                    FROM (
                        SELECT bot_id, created_at FROM inbound_queue
                        UNION ALL
                        SELECT bot_id, created_at FROM outbound_queue
                    )
                    WHERE created_at >= ? AND created_at < ?
                ) current_active
                ON current_active.bot_id = previous_active.bot_id
                """,
                (previous_start, current_start, current_start, now),
            ).fetchone()

            engaged_row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM (
                    SELECT bot_id, COUNT(*) AS events_count
                    FROM (
                        SELECT bot_id FROM inbound_queue WHERE created_at >= ?
                        UNION ALL
                        SELECT bot_id FROM outbound_queue WHERE created_at >= ?
                    )
                    GROUP BY bot_id
                    HAVING COUNT(*) >= ?
                )
                """,
                (current_start, current_start, clean_threshold),
            ).fetchone()

            bot_rows = conn.execute(
                """
                WITH current_events AS (
                    SELECT bot_id, status, created_at, 'inbound' AS queue_name
                    FROM inbound_queue
                    WHERE created_at >= ?
                    UNION ALL
                    SELECT bot_id, status, created_at, 'outbound' AS queue_name
                    FROM outbound_queue
                    WHERE created_at >= ?
                )
                SELECT
                    current_events.bot_id AS bot_id,
                    COALESCE(bots.name, current_events.bot_id) AS bot_name,
                    COUNT(*) AS events_count,
                    SUM(CASE WHEN current_events.queue_name = 'inbound' THEN 1 ELSE 0 END) AS inbound_count,
                    SUM(CASE WHEN current_events.queue_name = 'outbound' THEN 1 ELSE 0 END) AS outbound_count,
                    SUM(CASE WHEN current_events.status = 'done' THEN 1 ELSE 0 END) AS done_count,
                    SUM(CASE WHEN current_events.status IN ('done', 'error', 'dead_letter') THEN 1 ELSE 0 END) AS terminal_count,
                    MAX(current_events.created_at) AS last_event_at
                FROM current_events
                LEFT JOIN bots ON bots.bot_id = current_events.bot_id
                GROUP BY current_events.bot_id
                ORDER BY events_count DESC, current_events.bot_id ASC
                LIMIT ?
                """,
                (current_start, current_start, clean_limit),
            ).fetchall()

        active_24h = int((active_24h_row or {"c": 0})["c"] or 0)
        active_current = int((active_current_row or {"c": 0})["c"] or 0)
        previous_active = int((previous_active_row or {"c": 0})["c"] or 0)
        retained = int((retained_row or {"c": 0})["c"] or 0)
        engaged = int((engaged_row or {"c": 0})["c"] or 0)

        done_count = int((success_row or {"done_count": 0})["done_count"] or 0)
        terminal_count = int((success_row or {"terminal_count": 0})["terminal_count"] or 0)
        failed_count = max(0, terminal_count - done_count)

        success_rate_pct = (100.0 * float(done_count) / float(terminal_count)) if terminal_count > 0 else None
        retention_rate_pct = (100.0 * float(retained) / float(previous_active)) if previous_active > 0 else None
        engagement_rate_pct = (100.0 * float(engaged) / float(active_current)) if active_current > 0 else None

        bots: List[Dict[str, Any]] = []
        for row in bot_rows:
            events_count = int(row["events_count"] or 0)
            bot_terminal = int(row["terminal_count"] or 0)
            bot_done = int(row["done_count"] or 0)
            bots.append(
                {
                    "bot_id": str(row["bot_id"]),
                    "bot_name": str(row["bot_name"]),
                    "events_count": events_count,
                    "inbound_count": int(row["inbound_count"] or 0),
                    "outbound_count": int(row["outbound_count"] or 0),
                    "done_count": bot_done,
                    "failed_count": max(0, bot_terminal - bot_done),
                    "success_rate_pct": (100.0 * float(bot_done) / float(bot_terminal)) if bot_terminal > 0 else None,
                    "last_event_at": float(row["last_event_at"]) if row["last_event_at"] is not None else None,
                }
            )

        return {
            "generated_at": now,
            "window_days": clean_window_days,
            "engaged_event_threshold": clean_threshold,
            "kpis": {
                "active_agents_24h": active_24h,
                "active_agents_window": active_current,
                "retention_7d_pct": retention_rate_pct,
                "retention_7d": {
                    "retained_agents": retained,
                    "previous_active_agents": previous_active,
                    "current_active_agents": active_current,
                },
                "success_rate_window_pct": success_rate_pct,
                "success_rate_window": {
                    "done_events": done_count,
                    "failed_events": failed_count,
                    "terminal_events": terminal_count,
                },
                "engagement_rate_window_pct": engagement_rate_pct,
                "engagement_rate_window": {
                    "engaged_agents": engaged,
                    "active_agents_window": active_current,
                },
            },
            "bots": bots,
        }

    def list_dead_letter_events(self, queue: str = "both", limit: int = 100, bot_id: Optional[str] = None) -> Dict[str, Any]:
        clean_queue = str(queue or "both").strip().lower()
        if clean_queue not in {"inbound", "outbound", "both"}:
            clean_queue = "both"
        clean_limit = max(1, min(500, int(limit)))
        clean_bot_id = _safe_bot_id(bot_id) if bot_id else ""

        inbound_rows: List[sqlite3.Row] = []
        outbound_rows: List[sqlite3.Row] = []
        with self.lock, self._connect() as conn:
            if clean_queue in {"inbound", "both"}:
                if clean_bot_id:
                    inbound_rows = conn.execute(
                        """
                        SELECT *
                        FROM inbound_queue
                        WHERE status = 'dead_letter' AND bot_id = ?
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (clean_bot_id, clean_limit),
                    ).fetchall()
                else:
                    inbound_rows = conn.execute(
                        """
                        SELECT *
                        FROM inbound_queue
                        WHERE status = 'dead_letter'
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (clean_limit,),
                    ).fetchall()

            if clean_queue in {"outbound", "both"}:
                if clean_bot_id:
                    outbound_rows = conn.execute(
                        """
                        SELECT *
                        FROM outbound_queue
                        WHERE status = 'dead_letter' AND bot_id = ?
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (clean_bot_id, clean_limit),
                    ).fetchall()
                else:
                    outbound_rows = conn.execute(
                        """
                        SELECT *
                        FROM outbound_queue
                        WHERE status = 'dead_letter'
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (clean_limit,),
                    ).fetchall()

        inbound = [self._queue_row_to_dict(row) for row in inbound_rows]
        outbound = [self._queue_row_to_dict(row) for row in outbound_rows]
        return {
            "queue": clean_queue,
            "limit": clean_limit,
            "bot_id": clean_bot_id or None,
            "inbound": inbound,
            "outbound": outbound,
        }

    def replay_dead_letter_events(
        self,
        *,
        queue: str = "both",
        event_ids: Optional[List[str]] = None,
        bot_id: Optional[str] = None,
        limit: int = 100,
        reset_attempts: bool = False,
    ) -> Dict[str, Any]:
        clean_queue = str(queue or "both").strip().lower()
        if clean_queue not in {"inbound", "outbound", "both"}:
            clean_queue = "both"
        clean_limit = max(1, min(500, int(limit)))
        clean_bot_id = _safe_bot_id(bot_id) if bot_id else ""
        ids = [str(x).strip() for x in (event_ids or []) if str(x).strip()]
        seen: set[str] = set()
        unique_ids: List[str] = []
        for eid in ids:
            if eid not in seen:
                seen.add(eid)
                unique_ids.append(eid)

        now = time.time()
        replayed_inbound = 0
        replayed_outbound = 0

        with self.lock, self._connect() as conn:
            def _replay_table(table: str, selected_ids: Optional[List[str]]) -> int:
                attempts_sql = "attempts = 0," if bool(reset_attempts) else ""
                if selected_ids is not None:
                    if not selected_ids:
                        return 0
                    placeholders = ", ".join(["?"] * len(selected_ids))
                    params: List[Any] = [now, now, *selected_ids]
                    if clean_bot_id:
                        params.append(clean_bot_id)
                        cur = conn.execute(
                            f"""
                            UPDATE {table}
                            SET status = 'queued',
                                {attempts_sql}
                                error = '',
                                result_json = '',
                                available_at = ?,
                                locked_at = NULL,
                                updated_at = ?
                            WHERE status = 'dead_letter'
                              AND event_id IN ({placeholders})
                              AND bot_id = ?
                            """,
                            tuple(params),
                        )
                    else:
                        cur = conn.execute(
                            f"""
                            UPDATE {table}
                            SET status = 'queued',
                                {attempts_sql}
                                error = '',
                                result_json = '',
                                available_at = ?,
                                locked_at = NULL,
                                updated_at = ?
                            WHERE status = 'dead_letter'
                              AND event_id IN ({placeholders})
                            """,
                            tuple(params),
                        )
                    return int(cur.rowcount or 0)

                if clean_bot_id:
                    rows = conn.execute(
                        f"""
                        SELECT event_id
                        FROM {table}
                        WHERE status = 'dead_letter' AND bot_id = ?
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (clean_bot_id, clean_limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT event_id
                        FROM {table}
                        WHERE status = 'dead_letter'
                        ORDER BY updated_at DESC, id DESC
                        LIMIT ?
                        """,
                        (clean_limit,),
                    ).fetchall()
                event_list = [str(row["event_id"]) for row in rows]
                if not event_list:
                    return 0
                placeholders = ", ".join(["?"] * len(event_list))
                cur = conn.execute(
                    f"""
                    UPDATE {table}
                    SET status = 'queued',
                        {attempts_sql}
                        error = '',
                        result_json = '',
                        available_at = ?,
                        locked_at = NULL,
                        updated_at = ?
                    WHERE status = 'dead_letter'
                      AND event_id IN ({placeholders})
                    """,
                    (now, now, *event_list),
                )
                return int(cur.rowcount or 0)

            selected_ids = unique_ids if unique_ids else None
            if clean_queue in {"inbound", "both"}:
                replayed_inbound = _replay_table("inbound_queue", selected_ids)
            if clean_queue in {"outbound", "both"}:
                replayed_outbound = _replay_table("outbound_queue", selected_ids)
            conn.commit()

        return {
            "queue": clean_queue,
            "bot_id": clean_bot_id or None,
            "limit": clean_limit,
            "event_ids": unique_ids,
            "reset_attempts": bool(reset_attempts),
            "replayed_inbound": replayed_inbound,
            "replayed_outbound": replayed_outbound,
        }

    def purge_dead_letter_events(
        self,
        *,
        queue: str = "both",
        event_ids: Optional[List[str]] = None,
        bot_id: Optional[str] = None,
        limit: int = 100,
        older_than_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        clean_queue = str(queue or "both").strip().lower()
        if clean_queue not in {"inbound", "outbound", "both"}:
            clean_queue = "both"
        clean_limit = max(1, min(5000, int(limit)))
        clean_bot_id = _safe_bot_id(bot_id) if bot_id else ""
        ids = [str(x).strip() for x in (event_ids or []) if str(x).strip()]
        seen: set[str] = set()
        unique_ids: List[str] = []
        for eid in ids:
            if eid not in seen:
                seen.add(eid)
                unique_ids.append(eid)

        now = time.time()
        cutoff_ts = None
        if older_than_s is not None:
            cutoff_ts = now - float(max(0, int(older_than_s)))

        deleted_inbound = 0
        deleted_outbound = 0

        with self.lock, self._connect() as conn:
            def _delete_table(table: str, selected_ids: Optional[List[str]]) -> int:
                if selected_ids is not None:
                    if not selected_ids:
                        return 0
                    placeholders = ", ".join(["?"] * len(selected_ids))
                    conditions = ["status = 'dead_letter'", f"event_id IN ({placeholders})"]
                    params: List[Any] = [*selected_ids]
                    if clean_bot_id:
                        conditions.append("bot_id = ?")
                        params.append(clean_bot_id)
                    if cutoff_ts is not None:
                        conditions.append("updated_at <= ?")
                        params.append(float(cutoff_ts))
                    cur = conn.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE {' AND '.join(conditions)}
                        """,
                        tuple(params),
                    )
                    return int(cur.rowcount or 0)

                where_parts = ["status = 'dead_letter'"]
                where_params: List[Any] = []
                if clean_bot_id:
                    where_parts.append("bot_id = ?")
                    where_params.append(clean_bot_id)
                if cutoff_ts is not None:
                    where_parts.append("updated_at <= ?")
                    where_params.append(float(cutoff_ts))
                rows = conn.execute(
                    f"""
                    SELECT event_id
                    FROM {table}
                    WHERE {' AND '.join(where_parts)}
                    ORDER BY updated_at ASC, id ASC
                    LIMIT ?
                    """,
                    (*where_params, clean_limit),
                ).fetchall()
                chosen_ids = [str(row["event_id"]) for row in rows]
                if not chosen_ids:
                    return 0
                placeholders = ", ".join(["?"] * len(chosen_ids))
                cur = conn.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE event_id IN ({placeholders})
                    """,
                    tuple(chosen_ids),
                )
                return int(cur.rowcount or 0)

            selected_ids = unique_ids if unique_ids else None
            if clean_queue in {"inbound", "both"}:
                deleted_inbound = _delete_table("inbound_queue", selected_ids)
            if clean_queue in {"outbound", "both"}:
                deleted_outbound = _delete_table("outbound_queue", selected_ids)
            conn.commit()

        return {
            "queue": clean_queue,
            "bot_id": clean_bot_id or None,
            "limit": clean_limit,
            "older_than_s": int(older_than_s) if older_than_s is not None else None,
            "event_ids": unique_ids,
            "deleted_inbound": deleted_inbound,
            "deleted_outbound": deleted_outbound,
        }

    def enqueue_outbound_event(
        self,
        *,
        bot_id: str,
        channel: str,
        payload: Dict[str, Any],
        event_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_bot_id = _safe_bot_id(bot_id)
        clean_channel = str(channel or "").strip() or "unknown"
        payload_obj = payload if isinstance(payload, dict) else {}
        payload_text = json.dumps(payload_obj, ensure_ascii=True)
        now = time.time()
        eid = str(event_id or f"out_{uuid.uuid4().hex}").strip()
        idem = str(idempotency_key or eid).strip()
        if not clean_bot_id:
            raise RuntimeError("bot_id is required")
        with self.lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM outbound_queue WHERE idempotency_key = ?",
                (idem,),
            ).fetchone()
            if existing:
                return self._queue_row_to_dict(existing)
            conn.execute(
                """
                INSERT INTO outbound_queue(
                    event_id, idempotency_key, bot_id, channel, payload_json,
                    status, result_json, error, attempts, available_at, locked_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', '', '', 0, ?, NULL, ?, ?)
                """,
                (eid, idem, clean_bot_id, clean_channel, payload_text, now, now, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM outbound_queue WHERE event_id = ?", (eid,)).fetchone()
            if not row:
                raise RuntimeError("failed to enqueue outbound event")
            return self._queue_row_to_dict(row)

    def claim_next_outbound_event(self, worker_id: str, lock_timeout_s: int = 120) -> Optional[Dict[str, Any]]:
        now = time.time()
        worker_hint = str(worker_id or "worker").strip()[:48]
        stale_before = now - max(1, int(lock_timeout_s))
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_queue
                SET status = 'queued',
                    locked_at = NULL,
                    available_at = ?,
                    updated_at = ?
                WHERE status = 'processing' AND locked_at IS NOT NULL AND locked_at <= ?
                """,
                (now, now, stale_before),
            )
            row = conn.execute(
                """
                SELECT * FROM outbound_queue
                WHERE status = 'queued' AND available_at <= ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                """
                UPDATE outbound_queue
                SET status = 'processing',
                    attempts = attempts + 1,
                    locked_at = ?,
                    updated_at = ?,
                    error = ''
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, int(row["id"])),
            )
            if int(cur.rowcount or 0) != 1:
                conn.commit()
                return None
            conn.commit()
            claimed = conn.execute("SELECT * FROM outbound_queue WHERE id = ?", (int(row["id"]),)).fetchone()
            if not claimed:
                return None
            data = self._queue_row_to_dict(claimed)
            data["worker_id"] = worker_hint
            return data

    def complete_outbound_event(self, event_id: str, result: Dict[str, Any]) -> None:
        eid = str(event_id or "").strip()
        if not eid:
            return
        now = time.time()
        result_text = json.dumps(result if isinstance(result, dict) else {}, ensure_ascii=True)
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbound_queue
                SET status = 'done', result_json = ?, error = '', updated_at = ?
                WHERE event_id = ?
                """,
                (result_text, now, eid),
            )
            conn.commit()

    def fail_outbound_event(
        self,
        event_id: str,
        error: str,
        *,
        max_attempts: int = 1,
        retry_base_s: int = 1,
        retry_cap_s: int = 1,
    ) -> Dict[str, Any]:
        eid = str(event_id or "").strip()
        if not eid:
            return {}
        now = time.time()
        max_tries = max(1, int(max_attempts))
        clean_error = str(error or "unknown error")
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM outbound_queue WHERE event_id = ?", (eid,)).fetchone()
            if not row:
                return {}
            status = str(row["status"] or "").strip().lower()
            attempts = int(row["attempts"] or 0)

            if status == "processing" and attempts < max_tries:
                delay_s = self._compute_retry_delay_s(attempts, retry_base_s, retry_cap_s)
                conn.execute(
                    """
                    UPDATE outbound_queue
                    SET status = 'queued',
                        error = ?,
                        available_at = ?,
                        locked_at = NULL,
                        updated_at = ?
                    WHERE event_id = ?
                    """,
                    (clean_error, now + float(delay_s), now, eid),
                )
            else:
                terminal = "dead_letter" if status == "processing" and attempts >= max_tries else "error"
                conn.execute(
                    """
                    UPDATE outbound_queue
                    SET status = ?,
                        error = ?,
                        locked_at = NULL,
                        updated_at = ?
                    WHERE event_id = ?
                    """,
                    (terminal, clean_error, now, eid),
                )
            conn.commit()
            updated = conn.execute("SELECT * FROM outbound_queue WHERE event_id = ?", (eid,)).fetchone()
            return self._queue_row_to_dict(updated) if updated else {}

    def get_telegram_offset(self, bot_id: str) -> int:
        clean_bot_id = _safe_bot_id(bot_id)
        if not clean_bot_id:
            return 0
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT next_offset FROM telegram_poll_offsets WHERE bot_id = ?",
                (clean_bot_id,),
            ).fetchone()
            if not row:
                return 0
            return max(0, int(row["next_offset"] or 0))

    def set_telegram_offset(self, bot_id: str, next_offset: int) -> None:
        clean_bot_id = _safe_bot_id(bot_id)
        if not clean_bot_id:
            return
        offset = max(0, int(next_offset))
        now = time.time()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO telegram_poll_offsets(bot_id, next_offset, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    next_offset = excluded.next_offset,
                    updated_at = excluded.updated_at
                """,
                (clean_bot_id, offset, now),
            )
            conn.commit()


class DemoMcpServer:
    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "echo",
                "description": "Echoes text back to the caller.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "bot_id": {"type": "string"}},
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
            return {
                "ok": True,
                "text": str(arguments.get("text", "")),
                "bot_id": str(arguments.get("_bot_id", arguments.get("bot_id", ""))),
            }
        if name == "time_now":
            return {"ok": True, "ts": time.time(), "bot_id": str(arguments.get("_bot_id", arguments.get("bot_id", "")))}
        raise RuntimeError(f"Unknown demo tool: {name}")

    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "transport": "inproc-demo"}


class StdioMcpClient:
    def __init__(self, server_id: str, command: str, args: List[str], timeout_s: int, env: Optional[Dict[str, str]] = None):
        self.server_id = server_id
        self.command = command
        self.args = args
        self.timeout_s = timeout_s
        self.env = env or {}
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.initialized = False
        self.stderr_thread: Optional[threading.Thread] = None
        self.stderr_tail: List[str] = []
        self.stderr_tail_lock = threading.Lock()

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        merged_env = dict(os.environ)
        merged_env.update({str(k): str(v) for k, v in self.env.items()})
        self.proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        self.initialized = False
        self.stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"mcp-stderr-{self.server_id}",
            daemon=True,
        )
        self.stderr_thread.start()

    def _drain_stderr(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        try:
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                with self.stderr_tail_lock:
                    self.stderr_tail.append(text)
                    self.stderr_tail = self.stderr_tail[-50:]
        except Exception:
            return

    def _stderr_hint(self) -> str:
        with self.stderr_tail_lock:
            if not self.stderr_tail:
                return ""
            return " | stderr: " + " || ".join(self.stderr_tail[-3:])

    def _readline_with_timeout(self, fd: int, deadline: float) -> bytes:
        buf = bytearray()
        while True:
            now = time.time()
            if now >= deadline:
                raise RuntimeError(f"MCP server {self.server_id} header read timed out{self._stderr_hint()}")
            timeout = max(0.0, deadline - now)
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                continue
            chunk = os.read(fd, 1)
            if not chunk:
                raise RuntimeError(f"MCP server {self.server_id} closed stdout{self._stderr_hint()}")
            buf.extend(chunk)
            if buf.endswith(b"\n"):
                return bytes(buf)

    def _read_exact_with_timeout(self, fd: int, size: int, deadline: float) -> bytes:
        out = bytearray()
        while len(out) < size:
            now = time.time()
            if now >= deadline:
                raise RuntimeError(f"MCP server {self.server_id} body read timed out{self._stderr_hint()}")
            timeout = max(0.0, deadline - now)
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                continue
            chunk = os.read(fd, size - len(out))
            if not chunk:
                raise RuntimeError(f"MCP server {self.server_id} closed stdout{self._stderr_hint()}")
            out.extend(chunk)
        return bytes(out)

    def _write_message(self, payload: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError(f"MCP server {self.server_id} is not running")
        body = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
        self.proc.stdin.write(body)
        self.proc.stdin.flush()

    def _read_message(self, deadline: float) -> Dict[str, Any]:
        if not self.proc or not self.proc.stdout:
            raise RuntimeError(f"MCP server {self.server_id} is not running")
        fd = self.proc.stdout.fileno()
        while True:
            line = self._readline_with_timeout(fd=fd, deadline=deadline)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if text.lower().startswith("content-length:"):
                headers: Dict[str, str] = {}
                first = text
                if ":" in first:
                    k, v = first.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                while True:
                    hline = self._readline_with_timeout(fd=fd, deadline=deadline)
                    htxt = hline.decode("ascii", errors="ignore").strip()
                    if not htxt:
                        break
                    if ":" in htxt:
                        k, v = htxt.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                length = int(headers.get("content-length", "0"))
                if length <= 0:
                    raise RuntimeError(
                        f"MCP server {self.server_id} returned invalid content-length{self._stderr_hint()}"
                    )
                body = self._read_exact_with_timeout(fd=fd, size=length, deadline=deadline)
                if not body:
                    raise RuntimeError(
                        f"MCP server {self.server_id} returned empty response body{self._stderr_hint()}"
                    )
                data = json.loads(body.decode("utf-8"))
            else:
                try:
                    data = json.loads(text)
                except Exception:
                    continue
            if not isinstance(data, dict):
                raise RuntimeError(f"MCP server {self.server_id} returned invalid JSON-RPC payload{self._stderr_hint()}")
            return data

    def _request(self, method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        self._start()
        self.request_id += 1
        req_id = self.request_id
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write_message(payload)
        deadline = time.time() + self.timeout_s
        while True:
            if time.time() >= deadline:
                raise RuntimeError(f"MCP request timed out for {self.server_id}:{method}{self._stderr_hint()}")
            msg = self._read_message(deadline=deadline)
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
                "clientInfo": {"name": "simpleagent", "version": "0.1.0"},
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


class HttpMcpClient:
    def __init__(self, server_id: str, url: str, timeout_s: int):
        self.server_id = server_id
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.lock = threading.Lock()
        self.request_id = 0
        self.initialized = False

    def _request(self, method: str, params: Optional[Dict[str, Any]], expect_response: bool = True) -> Dict[str, Any]:
        self.request_id += 1
        req_id = self.request_id
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = requests.post(
            self.url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout_s,
        )
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()
            raise RuntimeError(f"MCP HTTP {self.server_id}:{method} error {resp.status_code}: {detail[:400] or 'request failed'}")
        if not expect_response:
            return {}
        data = resp.json() if resp.text else {}
        if not isinstance(data, dict):
            raise RuntimeError(f"MCP HTTP {self.server_id}:{method} returned invalid JSON")
        if data.get("error"):
            raise RuntimeError(f"MCP HTTP {self.server_id}:{method} error: {json.dumps(data['error'])}")
        return data

    def _ensure_initialized(self) -> None:
        if self.initialized:
            return
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "simpleagent", "version": "0.1.0"},
            },
        )
        if "result" not in result:
            raise RuntimeError(f"MCP initialize failed for {self.server_id}")
        self._request("notifications/initialized", None, expect_response=False)
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
            try:
                resp = requests.get(self.url.replace("/mcp", "/healthz"), timeout=min(self.timeout_s, 5))
                if resp.status_code < 400:
                    return {"status": "ok", "transport": "http"}
            except Exception:
                pass
            return {"status": "unknown", "transport": "http"}


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
            if not server_id:
                continue
            if transport == "stdio":
                if not command:
                    continue
                safe_args = [str(a) for a in args] if isinstance(args, list) else []
                env_obj = item.get("env")
                safe_env: Dict[str, str] = {}
                if isinstance(env_obj, dict):
                    safe_env = {str(k): str(v) for k, v in env_obj.items()}
                self.servers[server_id] = StdioMcpClient(
                    server_id=server_id,
                    command=command,
                    args=safe_args,
                    timeout_s=self.timeout_s,
                    env=safe_env,
                )
                continue
            if transport == "http":
                url = str(item.get("url", "")).strip()
                if not url:
                    continue
                self.servers[server_id] = HttpMcpClient(
                    server_id=server_id,
                    url=url,
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

    def call_tool(self, full_name: str, arguments: Dict[str, Any], bot_id: Optional[str] = None) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("MCP is disabled")
        name = str(full_name or "").strip()
        if "." not in name:
            raise RuntimeError("MCP tool name must be in 'server.tool' format")
        server_id, tool_name = name.split(".", 1)
        server = self.servers.get(server_id)
        if server is None:
            raise RuntimeError(f"MCP server '{server_id}' is not configured")

        args = dict(arguments or {})
        if bot_id:
            args["_bot_id"] = str(bot_id)
            args.setdefault("bot_id", str(bot_id))
        return server.call_tool(tool_name, args)

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

    session_db_path = _env_str_with_legacy("SIMPLEAGENT_DB_PATH", "simpleagent.db").strip() or "simpleagent.db"
    session_max_messages = _env_int_with_legacy("SIMPLEAGENT_SESSION_MAX_MESSAGES", 100)
    state = AppState(
        session_store=SqliteSessionStore(
            db_path=session_db_path,
            max_messages_per_session=session_max_messages,
        )
    )

    class _ToolEventStreamBroker:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._subscribers: Dict[str, List["queue.Queue[Dict[str, Any]]"]] = {}

        def subscribe(self, bot_id: str) -> "queue.Queue[Dict[str, Any]]":
            q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=200)
            key = _safe_bot_id(bot_id)
            with self._lock:
                self._subscribers.setdefault(key, []).append(q)
            return q

        def unsubscribe(self, bot_id: str, q: "queue.Queue[Dict[str, Any]]") -> None:
            key = _safe_bot_id(bot_id)
            with self._lock:
                queues = self._subscribers.get(key) or []
                if q in queues:
                    queues.remove(q)
                if not queues and key in self._subscribers:
                    del self._subscribers[key]

        def publish(self, bot_id: str, event: Dict[str, Any]) -> None:
            key = _safe_bot_id(bot_id)
            with self._lock:
                queues = list(self._subscribers.get(key) or [])
            for q in queues:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                    try:
                        q.put_nowait(event)
                    except Exception:
                        pass

    tool_event_stream_broker = _ToolEventStreamBroker()

    forward_enabled = _env_bool_with_legacy("SIMPLEAGENT_FORWARD_ENABLED", False)
    forward_url = _env_str_with_legacy("SIMPLEAGENT_FORWARD_URL", "").strip()
    forward_token = _env_str_with_legacy("SIMPLEAGENT_FORWARD_TOKEN", "").strip()
    default_model = _env_str_with_legacy("SIMPLEAGENT_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    llm_timeout_s = _env_int_with_legacy("SIMPLEAGENT_LLM_TIMEOUT_S", 60)
    llm_system_prompt = _env_str_with_legacy(
        "SIMPLEAGENT_SYSTEM_PROMPT",
        "You are SimpleAgent, a concise, practical operations assistant.",
    ).strip()
    shell_enabled = _env_bool_with_legacy("SIMPLEAGENT_SHELL_ENABLED", False)
    shell_cwd = _env_str_with_legacy("SIMPLEAGENT_SHELL_CWD", ".").strip() or "."
    shell_timeout_s = max(1, _env_int_with_legacy("SIMPLEAGENT_SHELL_TIMEOUT_S", 20))
    shell_max_output_chars = max(200, _env_int_with_legacy("SIMPLEAGENT_SHELL_MAX_OUTPUT_CHARS", 8000))
    shell_max_calls_per_turn = max(1, _env_int_with_legacy("SIMPLEAGENT_SHELL_MAX_CALLS_PER_TURN", 3))
    web_enabled = _env_bool_with_legacy("SIMPLEAGENT_WEB_ENABLED", True)
    web_timeout_s = max(1, _env_int_with_legacy("SIMPLEAGENT_WEB_TIMEOUT_S", 12))
    web_max_chars = max(500, _env_int_with_legacy("SIMPLEAGENT_WEB_MAX_CHARS", 6000))
    web_search_max_results = max(1, min(8, _env_int_with_legacy("SIMPLEAGENT_WEB_SEARCH_MAX_RESULTS", 5)))
    mcp_enabled = _env_bool_with_legacy("SIMPLEAGENT_MCP_ENABLED", True)
    mcp_timeout_s = max(1, _env_int_with_legacy("SIMPLEAGENT_MCP_TIMEOUT_S", 20))
    mcp_servers_json = _env_str_with_legacy("SIMPLEAGENT_MCP_SERVERS_JSON", "[]")
    mcp_disabled_tools_raw = _env_str_with_legacy("SIMPLEAGENT_MCP_DISABLED_TOOLS", "").strip()
    public_base_url = _env_str_with_legacy("SIMPLEAGENT_PUBLIC_BASE_URL", "").strip().rstrip("/")
    openai_api_key = (
        os.getenv("OPENAI_API_KEY", "").strip()
        or _env_str_with_legacy("SIMPLEAGENT_LLM_API_KEY", "").strip()
    )
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    google_api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    gateway_token = os.getenv("GATEWAY_TOKEN", "").strip() or os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
    gateway_tool_url = _env_str_with_legacy("SIMPLEAGENT_GATEWAY_TOOL_URL", "").strip()
    bot_context_root = _env_str_with_legacy("SIMPLEAGENT_BOT_CONTEXT_DIR", "./bot_context").strip() or "./bot_context"
    service_mode_raw = _env_str_with_legacy("SIMPLEAGENT_SERVICE_MODE", "all").strip().lower() or "all"
    service_mode = service_mode_raw if service_mode_raw in {"all", "gateway", "executor"} else "all"
    gateway_plane_enabled = service_mode in {"all", "gateway"}
    executor_plane_enabled = service_mode in {"all", "executor"}
    telegram_poller_enabled = _env_bool_with_legacy("SIMPLEAGENT_TELEGRAM_POLLER_ENABLED", service_mode == "gateway")
    executor_auto_run = _env_bool_with_legacy("SIMPLEAGENT_EXECUTOR_AUTO_RUN", service_mode == "executor")
    delivery_auto_run = _env_bool_with_legacy("SIMPLEAGENT_DELIVERY_AUTO_RUN", service_mode == "executor")
    queue_poll_interval_s = max(0.05, float(_env_int_with_legacy("SIMPLEAGENT_QUEUE_POLL_INTERVAL_MS", 200)) / 1000.0)
    chat_sync_timeout_s = max(1, _env_int_with_legacy("SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S", 35))
    telegram_poll_timeout_s = max(1, min(50, _env_int_with_legacy("SIMPLEAGENT_TELEGRAM_POLL_TIMEOUT_S", 25)))
    telegram_poll_retry_s = max(1, _env_int_with_legacy("SIMPLEAGENT_TELEGRAM_POLL_RETRY_S", 5))
    queue_lock_timeout_s = max(5, _env_int_with_legacy("SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S", 120))
    queue_max_attempts = max(1, _env_int_with_legacy("SIMPLEAGENT_QUEUE_MAX_ATTEMPTS", 5))
    queue_retry_base_s = max(1, _env_int_with_legacy("SIMPLEAGENT_QUEUE_RETRY_BASE_S", 2))
    queue_retry_cap_s = max(queue_retry_base_s, _env_int_with_legacy("SIMPLEAGENT_QUEUE_RETRY_CAP_S", 60))
    autoscale_inbound_target_ready_per_worker = max(1, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER", 20))
    autoscale_outbound_target_ready_per_worker = max(1, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_OUTBOUND_TARGET_READY_PER_WORKER", 40))
    autoscale_executor_min_workers = max(0, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_EXECUTOR_MIN_WORKERS", 0))
    autoscale_executor_max_workers = max(autoscale_executor_min_workers, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_EXECUTOR_MAX_WORKERS", 100))
    autoscale_delivery_min_workers = max(0, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_DELIVERY_MIN_WORKERS", 0))
    autoscale_delivery_max_workers = max(autoscale_delivery_min_workers, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_DELIVERY_MAX_WORKERS", 100))
    autoscale_max_step_up = max(1, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_MAX_STEP_UP", 10))
    autoscale_max_step_down = max(1, _env_int_with_legacy("SIMPLEAGENT_AUTOSCALE_MAX_STEP_DOWN", 10))
    runtime_kind = _env_str_with_legacy("SIMPLEAGENT_RUNTIME_KIND", "simpleagent_oss_local").strip() or "simpleagent_oss_local"
    runtime_version = _env_str_with_legacy("SIMPLEAGENT_RUNTIME_VERSION", "oss-v1").strip() or "oss-v1"
    runtime_contract_version = RUNTIME_CONTRACT_VERSION_V1
    runtime_tools_protocol = "tool-tag-v1"
    runtime_max_tool_passes = max(1, _env_int_with_legacy("SIMPLEAGENT_MAX_TOOL_PASSES", 4))
    runtime_max_turn_seconds = max(1, _env_int_with_legacy("SIMPLEAGENT_MAX_TURN_SECONDS", 90))
    runtime_max_tool_call_seconds = max(1, _env_int_with_legacy("SIMPLEAGENT_MAX_TOOL_CALL_SECONDS", 30))
    runtime_max_output_chars = max(200, _env_int_with_legacy("SIMPLEAGENT_MAX_OUTPUT_CHARS", 8000))

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

    shell_enabled_runtime = shell_enabled
    web_enabled_runtime = web_enabled
    mcp_enabled_runtime = mcp_enabled
    mcp_disabled_tools: set[str] = {
        t.strip()
        for t in mcp_disabled_tools_raw.split(",")
        if t.strip()
    }

    mcp_broker = McpBroker(enabled=True, timeout_s=mcp_timeout_s, servers_json=mcp_servers_json)
    gateway_cookie_name = "_simpleagent_gateway_token"

    def _bearer_token_from_request() -> str:
        auth_header = str(request.headers.get("Authorization", "")).strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return ""

    def _request_presents_gateway_token() -> bool:
        if not gateway_token:
            return True
        query_token = str(request.args.get("token", "")).strip()
        cookie_token = str(request.cookies.get(gateway_cookie_name, "")).strip()
        header_token = _bearer_token_from_request()
        return gateway_token in {query_token, cookie_token, header_token}

    def _is_public_without_gateway_token(path: str) -> bool:
        public_exact = {
            "/health",
            "/livez",
            "/readyz",
            "/api/livez",
            "/api/readyz",
            "/v1/health",
            "/v1/capabilities",
        }
        if path in public_exact:
            return True
        if path.startswith("/api/telegram/webhook/"):
            return True
        return False

    @app.before_request
    def _enforce_gateway_token_for_ui_and_api():
        if not gateway_token:
            return None
        if request.method == "OPTIONS":
            return None
        path = str(request.path or "")
        if _is_public_without_gateway_token(path):
            return None
        if not _request_presents_gateway_token():
            return jsonify({"status": "error", "error": "unauthorized"}), 401
        if str(request.args.get("token", "")).strip() == gateway_token:
            g._set_gateway_cookie = True
        return None

    @app.after_request
    def _persist_gateway_token_cookie(response: Response):
        if gateway_token and bool(getattr(g, "_set_gateway_cookie", False)):
            forwarded_proto = str(request.headers.get("X-Forwarded-Proto", "")).strip().lower()
            secure_cookie = bool(request.is_secure or forwarded_proto == "https")
            response.set_cookie(
                gateway_cookie_name,
                gateway_token,
                max_age=86400,
                httponly=True,
                secure=secure_cookie,
                samesite="Lax",
            )
        return response

    def _new_bot_id() -> str:
        return f"bot-{uuid.uuid4().hex[:12]}"

    def _mask_bot(bot: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "bot_id": bot["bot_id"],
            "name": bot["name"],
            "model": bot["model"],
            "telegram_enabled": bool(bot.get("telegram_bot_token")),
            "telegram_bot_token": _mask_secret(str(bot.get("telegram_bot_token", ""))),
            "telegram_owner_user_id": bot.get("telegram_owner_user_id"),
            "telegram_owner_chat_id": bot.get("telegram_owner_chat_id"),
            "heartbeat_interval_s": int(bot.get("heartbeat_interval_s") or 300),
            "created_at": float(bot.get("created_at") or 0.0),
            "updated_at": float(bot.get("updated_at") or 0.0),
        }

    CONTEXT_FILES = ("SOUL.md", "USER.md", "HEARTBEAT.md")
    CONTEXT_FILE_MAP = {name.upper(): name for name in CONTEXT_FILES}

    def _context_defaults(bot_id: str) -> Dict[str, str]:
        return {
            "SOUL.md": (
                f"# SOUL for {bot_id}\n\n"
                "Core identity, goals, and tone for this bot.\n"
                "Keep this concise and stable.\n"
            ),
            "USER.md": (
                f"# USER profile for {bot_id}\n\n"
                "Known user preferences and important constraints.\n"
                "Update this when persistent user behavior changes.\n"
            ),
            "HEARTBEAT.md": (
                f"# HEARTBEAT for {bot_id}\n\n"
                "Frequency: every 300 seconds\n"
                "Purpose: keep the always-on illusion while conserving compute.\n"
            ),
        }

    def _context_dir(bot_id: str) -> str:
        safe = _safe_bot_id(bot_id)
        if not safe:
            raise RuntimeError("invalid bot_id")
        return os.path.join(os.path.abspath(bot_context_root), safe)

    def _normalize_context_name(file_name: str) -> str:
        clean_key = str(file_name or "").strip().upper()
        normalized = CONTEXT_FILE_MAP.get(clean_key)
        if not normalized:
            raise RuntimeError("context file must be one of: SOUL.md, USER.md, HEARTBEAT.md")
        return normalized

    def _context_path(bot_id: str, file_name: str) -> str:
        normalized_name = _normalize_context_name(file_name)
        return os.path.join(_context_dir(bot_id), normalized_name)

    def _ensure_bot_context_files(bot_id: str) -> None:
        os.makedirs(_context_dir(bot_id), exist_ok=True)
        defaults = _context_defaults(bot_id)
        for name in CONTEXT_FILES:
            path = _context_path(bot_id, name)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(defaults[name])

    def _read_bot_context_file(bot_id: str, file_name: str) -> str:
        _ensure_bot_context_files(bot_id)
        path = _context_path(bot_id, file_name)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _write_bot_context_file(bot_id: str, file_name: str, content: str) -> None:
        _ensure_bot_context_files(bot_id)
        path = _context_path(bot_id, file_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(content or ""))

    def _load_bot_context(bot_id: str) -> Dict[str, str]:
        _ensure_bot_context_files(bot_id)
        return {name: _read_bot_context_file(bot_id, name) for name in CONTEXT_FILES}

    def _require_bot(bot_id: str) -> Dict[str, Any]:
        clean_id = _safe_bot_id(bot_id)
        if not clean_id:
            raise RuntimeError("bot_id is required")
        bot = state.session_store.get_bot(clean_id)
        if not bot:
            raise RuntimeError(f"bot_id '{clean_id}' was not found")
        _ensure_bot_context_files(clean_id)
        return bot

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

    def _forward(bot_id: str, message: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        outbound_payload = {
            "message": message,
            "event": payload if isinstance(payload, dict) else {},
        }
        delivery = _dispatch_outbound_event(
            bot_id=bot_id,
            channel="forward",
            payload=outbound_payload,
            idempotency_key=f"forward:{bot_id}:{uuid.uuid4().hex}",
        )
        if bool(delivery.get("ok")):
            state.set_last_forward(delivery)
        return delivery

    def _telegram_token_ready(token: str) -> bool:
        return bool(token) and not token.startswith("replace-with-")

    def _telegram_api(bot_token: str, method: str, payload: Dict[str, Any]) -> Any:
        token = str(bot_token or "").strip()
        if not token:
            raise RuntimeError("telegram_bot_token is required for Telegram integration")
        url = f"https://api.telegram.org/bot{token}/{method}"
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json() if resp.text else {}
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {json.dumps(data, ensure_ascii=True)}")
        return data.get("result")

    def _telegram_send_message(bot_token: str, chat_id: int, text: str, reply_to_message_id: Optional[int]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return _telegram_api(bot_token, "sendMessage", payload)

    def _telegram_get_updates(bot_token: str, offset: int, timeout_s: int) -> List[Dict[str, Any]]:
        result = _telegram_api(
            bot_token,
            "getUpdates",
            {
                "offset": int(offset),
                "timeout": int(timeout_s),
                "allowed_updates": ["message"],
            },
        )
        return result if isinstance(result, list) else []

    def _deliver_outbound_event(outbound_event: Dict[str, Any]) -> Dict[str, Any]:
        channel = str(outbound_event.get("channel", "")).strip()
        bot_id = str(outbound_event.get("bot_id", "")).strip()
        payload = outbound_event.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if channel == "forward":
            if not forward_enabled:
                return {"ok": False, "message": "forwarding disabled", "bot_id": bot_id}
            if not forward_url:
                raise RuntimeError("SIMPLEAGENT_FORWARD_URL is required when SIMPLEAGENT_FORWARD_ENABLED=1")
            message = str(payload.get("message", "")).strip()
            event_payload = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            headers = {"Content-Type": "application/json"}
            if forward_token:
                headers["Authorization"] = f"Bearer {forward_token}"
            body = {
                "source": "simpleagent",
                "bot_id": bot_id,
                "message": message,
                "event": event_payload,
                "received_at": time.time(),
            }
            resp = requests.post(forward_url, json=body, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json() if resp.text else {}
            return {"ok": True, "bot_id": bot_id, "forwarded_at": time.time(), "response": data}

        if channel == "telegram_send":
            bot_token = str(payload.get("bot_token", "")).strip()
            chat_id = int(payload.get("chat_id"))
            text = str(payload.get("text", ""))
            reply_to_message_id_raw = payload.get("reply_to_message_id")
            reply_to_message_id = (
                int(reply_to_message_id_raw)
                if isinstance(reply_to_message_id_raw, (int, str)) and str(reply_to_message_id_raw).strip()
                else None
            )
            sent = _telegram_send_message(bot_token=bot_token, chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id)
            return {"ok": True, "bot_id": bot_id, "channel": "telegram_send", "telegram_result": sent}

        raise RuntimeError(f"Unknown outbound channel: {channel}")

    def _dispatch_outbound_event(
        *,
        bot_id: str,
        channel: str,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        outbound_event = state.session_store.enqueue_outbound_event(
            bot_id=bot_id,
            channel=channel,
            payload=payload if isinstance(payload, dict) else {},
            idempotency_key=idempotency_key,
        )

        if service_mode == "all":
            try:
                result = _deliver_outbound_event(outbound_event)
                state.session_store.complete_outbound_event(outbound_event["event_id"], result)
                return result
            except Exception as exc:
                state.session_store.fail_outbound_event(outbound_event["event_id"], str(exc))
                raise

        return {
            "ok": True,
            "queued": True,
            "bot_id": bot_id,
            "channel": channel,
            "outbound_event_id": outbound_event["event_id"],
            "delivery_status": "queued",
        }

    def _get_callback_url() -> str:
        if public_base_url:
            return f"{public_base_url}/hooks/outward_inbox"
        host = (os.getenv("HOST", "127.0.0.1") or "127.0.0.1").strip()
        if host in {"0.0.0.0", "::"}:
            host = "localhost"
        port = str(os.getenv("PORT", "18789")).strip() or "18789"
        return f"http://{host}:{port}/hooks/outward_inbox"

    def _append_user_message_if_missing(bot_id: str, session_id: str, user_message: str) -> None:
        history = state.get_session(bot_id=bot_id, session_id=session_id)
        if history:
            last = history[-1]
            if (
                str(last.get("role", "")).strip() == "user"
                and str(last.get("content", "")) == user_message
            ):
                return
        state.append_session(bot_id=bot_id, session_id=session_id, role="user", content=user_message)

    def _build_llm_messages(bot: Dict[str, Any], session_id: str, user_message: str) -> List[Dict[str, str]]:
        bot_id = str(bot["bot_id"])
        history = state.get_session(bot_id=bot_id, session_id=session_id)[-20:]
        context_files = _load_bot_context(bot_id)

        effective_system_prompt = (
            f"{llm_system_prompt}\n\n"
            f"BOT_ID: {bot_id}\n"
            "All tool calls and downstream actions must use this BOT_ID for strict tenant isolation.\n\n"
            "Per-bot context files loaded for this response:\n"
            "[SOUL.md]\n"
            f"{_truncate_text(context_files.get('SOUL.md', ''), 4000)}\n\n"
            "[USER.md]\n"
            f"{_truncate_text(context_files.get('USER.md', ''), 4000)}\n\n"
            "[HEARTBEAT.md]\n"
            f"{_truncate_text(context_files.get('HEARTBEAT.md', ''), 4000)}"
        )

        effective_system_prompt = (
            f"{effective_system_prompt}\n\n"
            "Execution behavior:\n"
            "- For direct action requests, act immediately by using a tool when one is available.\n"
            "- Do not ask for continuation confirmations like 'ok', 'go on', or 'should I proceed?'.\n"
            "- Do not narrate intent such as 'I will do that now' without taking action.\n"
            "- Ask follow-up questions only when blocked by missing required information or explicit irreversible-action approval.\n"
            "- When follow-up is required, ask once and include all missing required fields in a single message."
        )

        mcp_tools = _get_active_mcp_tools()
        if shell_enabled_runtime:
            effective_system_prompt = (
                f"{effective_system_prompt}\n\n"
                "You can request shell access with exactly one line:\n"
                "<tool:shell>your shell command</tool:shell>\n"
                "Rules:\n"
                "- Use shell only when required.\n"
                "- Keep commands minimal and non-interactive.\n"
                "- Do not wrap tool lines in markdown."
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
                    "You can request MCP tools with exactly one line:\n"
                    "<tool:mcp name=\"server.tool\">{\"arg\":\"value\"}</tool:mcp>\n"
                    "The platform automatically attaches BOT_ID to MCP arguments.\n"
                    "Available MCP tools:\n"
                    + "\n".join(tool_lines)
                )
        if web_enabled_runtime:
            effective_system_prompt = (
                f"{effective_system_prompt}\n\n"
                "You can request web tools:\n"
                "<tool:web_search>query text</tool:web_search>\n"
                "<tool:web_fetch>https://example.com/page</tool:web_fetch>\n"
                "Rules:\n"
                "- Use web tools only when needed for current information.\n"
                "- Use full http/https URLs.\n"
                "- Do not wrap tool lines in markdown."
            )

        effective_system_prompt = (
            f"{effective_system_prompt}\n\n"
            "You can fetch this agent's callback URL with:\n"
            "get_callback_url()\n\n"
            "You can use gateway heartbeat tools:\n"
            "<tool:gateway>{\"action\":\"heartbeat_get\"}</tool:gateway>\n"
            "<tool:gateway>{\"action\":\"heartbeat_set\",\"content\":\"...\",\"interval_s\":300}</tool:gateway>\n"
            "Rules:\n"
            "- Always preserve BOT_ID in gateway operations.\n"
            "- Do not wrap tool lines in markdown."
        )

        if _telegram_token_ready(str(bot.get("telegram_bot_token", ""))):
            effective_system_prompt = (
                f"{effective_system_prompt}\n\n"
                "You can send a Telegram message with exactly one line:\n"
                "send_telegram_messege(\"your message\")\n"
                "Rules:\n"
                "- Only for Telegram sessions (session_id starts with telegram:).\n"
                "- Do not wrap tool lines in markdown."
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
        tool_name = ""
        args_text = ""
        if match:
            tool_name = match.group(1).strip()
            args_text = match.group(2).strip() or "{}"
        else:
            direct_match = re.search(
                r"<tool:([A-Za-z0-9_.-]+)>\s*(.*?)\s*</tool:\1>",
                text,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not direct_match:
                return None
            direct_name = direct_match.group(1).strip()
            if not direct_name:
                return None
            normalized_name = direct_name.lower()
            if normalized_name in {
                "mcp",
                "web_search",
                "web_fetch",
                "shell",
                "gateway",
                "current_time",
            }:
                return None
            tool_name = direct_name
            args_text = direct_match.group(2).strip() or "{}"

        try:
            args_obj = json.loads(args_text)
        except Exception as exc:
            raise RuntimeError(f"Invalid MCP tool arguments JSON: {exc}") from exc
        if not isinstance(args_obj, dict):
            raise RuntimeError("MCP tool arguments must be a JSON object")
        return tool_name, args_obj

    def _extract_gateway_tool_call(text: str) -> Optional[Dict[str, Any]]:
        match = re.search(r"<tool:gateway>\s*(.*?)\s*</tool:gateway>", text, flags=re.DOTALL)
        if match:
            raw = match.group(1).strip() or "{}"
            try:
                payload = json.loads(raw)
            except Exception as exc:
                raise RuntimeError(f"Invalid gateway tool JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("gateway tool payload must be a JSON object")
            return payload

        if re.search(r"\bget_heartbeat\s*\(\s*\)", text):
            return {"action": "heartbeat_get"}
        call = re.search(r"set_heartbeat\s*\(\s*(.+?)\s*\)", text, flags=re.DOTALL)
        if call:
            raw = call.group(1).strip()
            if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
                raw = raw[1:-1]
            return {"action": "heartbeat_set", "content": raw}
        return None

    def _extract_web_search_query(text: str) -> Optional[str]:
        match = re.search(r"<tool:web_search>(.+?)</tool:web_search>", text, flags=re.DOTALL)
        if not match:
            return None
        query = match.group(1).strip()
        return query or None

    def _extract_web_fetch_url(text: str) -> Optional[str]:
        match = re.search(r"<tool:web_fetch>(.+?)</tool:web_fetch>", text, flags=re.DOTALL)
        if not match:
            return None
        url = match.group(1).strip()
        return url or None

    def _extract_telegram_send_call(text: str) -> Optional[str]:
        match = re.search(
            r"send_telegram_messege\s*\(\s*(.+?)\s*\)",
            text,
            flags=re.DOTALL,
        )
        if not match:
            match = re.search(
                r"send_telegram_message\s*\(\s*(.+?)\s*\)",
                text,
                flags=re.DOTALL,
            )
        if not match:
            return None
        raw = match.group(1).strip()
        if not raw:
            raise RuntimeError("send_telegram_messege requires a message argument")
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            return raw[1:-1]
        return raw

    def _extract_get_callback_url_call(text: str) -> bool:
        return bool(re.search(r"\bget_callback_url\s*\(\s*\)", text))

    _STATEFUL_REQUEST_RE = re.compile(
        r"\b("
        r"create|add|remove|delete|update|edit|configure|approve|pair|connect|"
        r"list|show|get|fetch|check|status|did you|set up|setup|start|stop"
        r")\b",
        flags=re.IGNORECASE,
    )
    _BACKEND_ENTITY_RE = re.compile(
        r"\b("
        r"task|tasks|camera|device|telegram|pairing|stream|feed|mcp|settings|key|token"
        r")\b",
        flags=re.IGNORECASE,
    )
    _COMPLETION_CLAIM_RE = re.compile(
        r"\b("
        r"done|created|added|removed|deleted|updated|approved|paired|configured|"
        r"current\s+task\s+status|task\s+id|status:\s|successfully"
        r")\b",
        flags=re.IGNORECASE,
    )

    def _mcp_verification_required(user_message: str, assistant_text: str) -> bool:
        if not _STATEFUL_REQUEST_RE.search(user_message or ""):
            return False
        if not _BACKEND_ENTITY_RE.search(user_message or ""):
            return False
        return bool(_COMPLETION_CLAIM_RE.search(assistant_text or ""))

    def _is_private_or_local_host(hostname: str) -> bool:
        host = hostname.strip().lower().strip(".")
        if not host:
            return True
        if host in {"localhost", "host.docker.internal"}:
            return True
        if host.endswith(".local") or host.endswith(".internal"):
            return True
        try:
            ip = ipaddress.ip_address(host)
            return bool(
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            )
        except ValueError:
            return False

    def _validate_public_http_url(raw_url: str) -> str:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("web_fetch URL must use http or https")
        if not parsed.hostname:
            raise RuntimeError("web_fetch URL is missing hostname")
        if _is_private_or_local_host(parsed.hostname):
            raise RuntimeError("web_fetch blocked for local or private hosts")
        return raw_url.strip()

    def _strip_html_tags(value: str) -> str:
        return re.sub(r"<[^>]+>", " ", value or "", flags=re.DOTALL)

    def _normalize_result_url(raw_href: str) -> str:
        href = unescape(raw_href.strip())
        if not href:
            return ""
        if href.startswith("/l/?"):
            parsed = urlparse(href)
            query_text = parsed.query.replace("&amp;", "&")
            uddg = parse_qs(query_text).get("uddg", [])
            if uddg:
                href = unquote(str(uddg[0]))
        if href.startswith("//"):
            href = f"https:{href}"
        parsed = urlparse(href)
        if (
            parsed.scheme in {"http", "https"}
            and (parsed.hostname or "").strip().lower().endswith("duckduckgo.com")
            and str(parsed.path or "").strip().startswith("/l/")
        ):
            query_text = str(parsed.query or "").replace("&amp;", "&")
            uddg = parse_qs(query_text).get("uddg", [])
            if uddg:
                href = unquote(str(uddg[0]))
                parsed = urlparse(href)
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if _is_private_or_local_host(parsed.hostname):
            return ""
        return href

    def _extract_ddg_search_results(html: str, max_results: int) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen: set[str] = set()

        for anchor in re.finditer(
            r"<a\s+([^>]*?)>(.*?)</a>",
            html or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            attrs = str(anchor.group(1) or "")
            class_match = re.search(
                r"""class\s*=\s*(["'])(.*?)\1""",
                attrs,
                flags=re.IGNORECASE | re.DOTALL,
            )
            classes = str((class_match.group(2) if class_match else "") or "").lower()
            if "result__a" not in classes and "result-link" not in classes:
                continue

            href_match = re.search(
                r"""href\s*=\s*(["'])(.*?)\1""",
                attrs,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not href_match:
                continue
            url = _normalize_result_url(str(href_match.group(2) or ""))
            if not url or url in seen:
                continue

            title = unescape(_strip_html_tags(anchor.group(2))).strip()
            if not title:
                title = url
            results.append({"title": title[:240], "url": url})
            seen.add(url)
            if len(results) >= max(1, int(max_results)):
                break

        return results

    def _is_ddg_challenge_page(html: str) -> bool:
        body = (html or "").lower()
        if not body:
            return False
        return (
            "anomaly-modal" in body
            or "anomaly.js" in body
            or "bots use duckduckgo too" in body
        )

    def _extract_jina_ddg_results(markdown_text: str, max_results: int) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen: set[str] = set()
        for match in re.finditer(
            r"^\s*\d+\.\[(.*?)\]\((https?://[^\s)]+)\)",
            markdown_text or "",
            flags=re.MULTILINE,
        ):
            title = unescape(str(match.group(1) or "")).strip()
            raw_url = unescape(str(match.group(2) or "")).strip()
            url = _normalize_result_url(raw_url)
            if not url or url in seen:
                continue
            results.append({"title": (title or url)[:240], "url": url})
            seen.add(url)
            if len(results) >= max(1, int(max_results)):
                break
        return results

    def _extract_bing_rss_results(xml_text: str, max_results: int) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen: set[str] = set()
        for item in re.finditer(
            r"<item\b[^>]*>(.*?)</item>",
            xml_text or "",
            flags=re.IGNORECASE | re.DOTALL,
        ):
            item_body = str(item.group(1) or "")
            title_match = re.search(
                r"<title\b[^>]*>(.*?)</title>",
                item_body,
                flags=re.IGNORECASE | re.DOTALL,
            )
            link_match = re.search(
                r"<link\b[^>]*>(.*?)</link>",
                item_body,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not link_match:
                continue
            raw_url = unescape(str(link_match.group(1) or "")).strip()
            url = _normalize_result_url(raw_url)
            if not url or url in seen:
                continue
            title = _strip_html_tags(unescape(str((title_match.group(1) if title_match else "") or ""))).strip()
            results.append({"title": (title or url)[:240], "url": url})
            seen.add(url)
            if len(results) >= max(1, int(max_results)):
                break
        return results

    def _web_search(query: str) -> Dict[str, Any]:
        if not web_enabled_runtime:
            raise RuntimeError("Web tool access is disabled by configuration.")
        q = query.strip()
        if not q:
            raise RuntimeError("web_search query is empty")
        browser_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        search_attempts: List[Dict[str, Any]] = [
            {
                "source": "duckduckgo_html",
                "url": f"https://duckduckgo.com/html/?q={quote_plus(q)}",
                "parser": _extract_ddg_search_results,
                "challenge_detector": _is_ddg_challenge_page,
            },
            {
                "source": "duckduckgo_lite",
                "url": f"https://lite.duckduckgo.com/lite/?q={quote_plus(q)}",
                "parser": _extract_ddg_search_results,
                "challenge_detector": _is_ddg_challenge_page,
            },
            {
                # Fallback mirror for DDG result pages when DDG serves anti-bot challenge HTML.
                "source": "duckduckgo_lite_via_jina",
                "url": f"https://r.jina.ai/http://lite.duckduckgo.com/lite/?q={quote_plus(q)}",
                "parser": _extract_jina_ddg_results,
                "challenge_detector": None,
            },
            {
                "source": "bing_rss",
                "url": f"https://www.bing.com/search?format=rss&q={quote_plus(q)}",
                "parser": _extract_bing_rss_results,
                "challenge_detector": None,
            },
        ]
        last_source = "duckduckgo_html"
        last_error: Optional[Exception] = None
        successful_fetch = False
        for attempt in search_attempts:
            source_name = str(attempt.get("source", "")).strip() or last_source
            search_url = str(attempt.get("url", "")).strip()
            parser = attempt.get("parser")
            challenge_detector = attempt.get("challenge_detector")
            last_source = source_name
            try:
                resp = requests.get(
                    search_url,
                    timeout=web_timeout_s,
                    headers={
                        "User-Agent": browser_ua,
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                resp.raise_for_status()
            except Exception as exc:
                last_error = exc
                continue
            successful_fetch = True
            body = resp.text or ""
            if callable(challenge_detector) and challenge_detector(body):
                continue
            if not callable(parser):
                continue
            results = parser(body, web_search_max_results)
            if results:
                return {"query": q, "results": results, "source": source_name}
        if not successful_fetch and last_error is not None:
            raise RuntimeError(f"web_search failed: {last_error}")
        return {"query": q, "results": [], "source": last_source}

    def _html_to_markdown(html: str) -> str:
        text = html or ""
        text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(
            r'<a[^>]*href=[\'\"]([^\'\"]+)[\'\"][^>]*>(.*?)</a>',
            lambda m: f"[{unescape(_strip_html_tags(m.group(2))).strip() or m.group(1)}]({m.group(1).strip()})",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        for level in range(6, 0, -1):
            tag = f"h{level}"
            text = re.sub(
                rf"<{tag}[^>]*>(.*?)</{tag}>",
                lambda m, n=level: f"\n{'#' * n} {unescape(_strip_html_tags(m.group(1))).strip()}\n",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
        text = re.sub(
            r"<li[^>]*>(.*?)</li>",
            lambda m: f"\n- {unescape(_strip_html_tags(m.group(1))).strip()}",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(r"</?(p|div|section|article|ul|ol|blockquote)[^>]*>", "\n", text, flags=re.IGNORECASE)

        text = _strip_html_tags(text)
        text = unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _web_fetch(raw_url: str) -> Dict[str, Any]:
        if not web_enabled_runtime:
            raise RuntimeError("Web tool access is disabled by configuration.")
        url = _validate_public_http_url(raw_url)
        resp = requests.get(
            url,
            timeout=web_timeout_s,
            headers={"User-Agent": "simpleagent/1.0"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        final_url = _validate_public_http_url(resp.url or url)
        content_type = str((resp.headers or {}).get("Content-Type", "")).lower()
        body = resp.text or ""
        title = ""
        if "<title" in body.lower():
            m = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
            if m:
                title = unescape(_strip_html_tags(m.group(1))).strip()[:240]
        content = _html_to_markdown(body) if ("html" in content_type or "<html" in body.lower()) else body.strip()
        content = _truncate_text(content, web_max_chars)
        return {
            "url": final_url,
            "status_code": int(resp.status_code),
            "content_type": content_type or "unknown",
            "title": title,
            "content": content,
        }

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

    def _resolve_model(selected_model: Optional[str], bot: Dict[str, Any]) -> str:
        model = str(selected_model or "").strip()
        if model:
            return model
        bot_model = str(bot.get("model", "")).strip()
        return bot_model or default_model

    def _resolve_provider_and_model(selected_model: Optional[str], bot: Dict[str, Any]) -> Tuple[str, str]:
        model = _resolve_model(selected_model, bot=bot)
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

    _MODEL_CONTEXT_LIMITS_TOKENS: Dict[str, int] = {
        "gpt-4o-mini": 128_000,
        "gpt-5.3-codex": 200_000,
        "gpt-5-thinking-high": 200_000,
        "gpt-5-mini": 200_000,
        "gpt-5.2-pro": 200_000,
        "claude-opus-4-6": 200_000,
        "claude-sonnet-4-6": 200_000,
        "claude-haiku-4-5": 200_000,
        "gemini-3.1-pro": 1_000_000,
        "gemini-3-flash": 1_000_000,
    }

    def _max_context_tokens_for_model(model_id: str) -> int:
        clean = str(model_id or "").strip()
        if not clean:
            return 128_000
        exact = _MODEL_CONTEXT_LIMITS_TOKENS.get(clean)
        if exact:
            return int(exact)
        lowered = clean.lower()
        if lowered.startswith("gpt-4o"):
            return 128_000
        if lowered.startswith("gpt-5") or lowered.startswith("o1") or lowered.startswith("o3") or "codex" in lowered:
            return 200_000
        if lowered.startswith("claude"):
            return 200_000
        if lowered.startswith("gemini"):
            return 1_000_000
        return 128_000

    def _estimate_tokens_for_messages(messages: List[Dict[str, str]]) -> int:
        # Lightweight approximation for UX/debugging; avoids provider-specific tokenizers.
        total = 2
        for message in messages:
            role = str(message.get("role", "")).strip()
            content = str(message.get("content", "")).strip()
            total += 4
            if role:
                total += max(1, int(math.ceil(len(role) / 4.0)))
            if content:
                total += max(1, int(math.ceil(len(content) / 4.0)))
        return max(0, int(total))

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
        output_text = str(payload.get("output_text", "")).strip()
        if output_text:
            return output_text

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
        should_try_responses_first = (
            lower.startswith("gpt-5")
            or "codex" in lower
            or lower.startswith("o1")
            or lower.startswith("o3")
        )

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

    def _call_llm(messages: List[Dict[str, str]], selected_model: Optional[str], bot: Dict[str, Any]) -> str:
        provider, model_name = _resolve_provider_and_model(selected_model, bot=bot)
        if provider == "anthropic":
            return _call_anthropic(messages=messages, model_name=model_name)
        if provider == "google":
            return _call_google(messages=messages, model_name=model_name)
        return _call_openai(messages=messages, model_name=model_name)

    def _gateway_request(bot_id: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = {
            "bot_id": bot_id,
            "action": str(action or "").strip(),
            "payload": payload,
            "ts": time.time(),
        }
        if not gateway_tool_url:
            return {"ok": False, "skipped": True, "reason": "SIMPLEAGENT_GATEWAY_TOOL_URL is not configured", "request": body}

        headers = {"Content-Type": "application/json"}
        if gateway_token:
            headers["Authorization"] = f"Bearer {gateway_token}"
        resp = requests.post(gateway_tool_url, json=body, headers=headers, timeout=20)
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()
            raise RuntimeError(f"Gateway tool call failed with {resp.status_code}: {detail[:400] or 'request failed'}")
        data = resp.json() if resp.text else {}
        return {"ok": True, "skipped": False, "request": body, "response": data}

    def _handle_gateway_tool(bot_id: str, call: Dict[str, Any]) -> Dict[str, Any]:
        action = str(call.get("action", "")).strip().lower()
        if not action:
            raise RuntimeError("gateway tool requires an 'action'")

        if action in {"heartbeat_get", "get_heartbeat"}:
            bot = _require_bot(bot_id)
            heartbeat = _read_bot_context_file(bot_id, "HEARTBEAT.md")
            payload = {
                "bot_id": bot_id,
                "heartbeat": heartbeat,
                "heartbeat_interval_s": int(bot.get("heartbeat_interval_s") or 300),
            }
            result = {
                "ok": True,
                "action": "heartbeat_get",
                "bot_id": bot_id,
                "heartbeat": heartbeat,
                "heartbeat_interval_s": int(bot.get("heartbeat_interval_s") or 300),
            }
            gateway_result = _gateway_request(bot_id, "heartbeat_get", payload)
            result["gateway"] = gateway_result
            return result

        if action in {"heartbeat_set", "set_heartbeat"}:
            content = str(call.get("content", call.get("heartbeat", ""))).strip()
            if not content:
                raise RuntimeError("heartbeat_set requires non-empty 'content'")
            _write_bot_context_file(bot_id, "HEARTBEAT.md", content)

            interval_val = call.get("interval_s")
            updated_bot: Optional[Dict[str, Any]] = None
            if interval_val is not None:
                updated_bot = state.session_store.update_bot(bot_id, heartbeat_interval_s=max(1, int(interval_val)))
            if updated_bot is None:
                updated_bot = _require_bot(bot_id)

            payload = {
                "bot_id": bot_id,
                "heartbeat": content,
                "heartbeat_interval_s": int(updated_bot.get("heartbeat_interval_s") or 300),
            }
            gateway_result = _gateway_request(bot_id, "heartbeat_set", payload)
            return {
                "ok": True,
                "action": "heartbeat_set",
                "bot_id": bot_id,
                "heartbeat": content,
                "heartbeat_interval_s": payload["heartbeat_interval_s"],
                "gateway": gateway_result,
            }

        payload = {k: v for k, v in call.items() if k != "action"}
        gateway_result = _gateway_request(bot_id, action, payload)
        return {
            "ok": True,
            "action": action,
            "bot_id": bot_id,
            "gateway": gateway_result,
        }

    def _send_telegram_tool_message(bot: Dict[str, Any], current_session_id: str, message_text: str) -> Dict[str, Any]:
        token = str(bot.get("telegram_bot_token", "")).strip()
        if not _telegram_token_ready(token):
            raise RuntimeError("telegram_bot_token is not configured for this BOT_ID")
        msg = str(message_text or "").strip()
        if not msg:
            raise RuntimeError("send_telegram_messege requires a non-empty message")
        if not current_session_id.startswith("telegram:"):
            raise RuntimeError(
                "send_telegram_messege requires a Telegram session (session_id like telegram:<chat_id>)"
            )
        chat_id_part = current_session_id.split(":", 1)[1].strip()
        if not chat_id_part:
            raise RuntimeError("Invalid Telegram session id")
        try:
            chat_id = int(chat_id_part)
        except ValueError as exc:
            raise RuntimeError("Invalid Telegram chat id in session") from exc
        delivery = _dispatch_outbound_event(
            bot_id=str(bot["bot_id"]),
            channel="telegram_send",
            payload={
                "bot_token": token,
                "chat_id": chat_id,
                "text": msg,
                "reply_to_message_id": None,
            },
            idempotency_key=f"tgtool:{bot.get('bot_id')}:{chat_id}:{uuid.uuid4().hex}",
        )
        return {
            "ok": True,
            "bot_id": str(bot["bot_id"]),
            "chat_id": chat_id,
            "message": msg,
            "telegram_result": delivery.get("telegram_result"),
            "delivery": delivery,
        }

    def _is_tool_allowed(
        *,
        tool_name: str,
        enabled_tools: Optional[List[str]],
        disabled_tools_policy: Optional[List[str]],
    ) -> bool:
        normalized_name = str(tool_name or "").strip()
        disabled_set = {str(item or "").strip() for item in (disabled_tools_policy or []) if str(item or "").strip()}
        if normalized_name in disabled_set:
            return False
        enabled_set = {str(item or "").strip() for item in (enabled_tools or []) if str(item or "").strip()}
        if not enabled_set:
            return True
        return normalized_name in enabled_set

    def _resolve_contract_tool_call(assistant_text: str, step: int) -> Optional[ToolInvocation]:
        invocation = parse_tool_tag_v1(assistant_text, step)
        if invocation is not None:
            return invocation

        if _extract_get_callback_url_call(assistant_text):
            return ToolInvocation(
                call_id=f"tc_{step}",
                name="get_callback_url",
                source="gateway",
                arguments={},
                step=step,
            )

        telegram_message = _extract_telegram_send_call(assistant_text)
        if telegram_message is not None:
            return ToolInvocation(
                call_id=f"tc_{step}",
                name="send_telegram_messege",
                source="gateway",
                arguments={"message": telegram_message},
                step=step,
            )
        return None

    def _execute_contract_tool_call(
        *,
        bot: Dict[str, Any],
        session_id: str,
        invocation: ToolInvocation,
        enabled_tools: Optional[List[str]] = None,
        disabled_tools_policy: Optional[List[str]] = None,
    ) -> ToolResultEnvelope:
        def _bounded_envelope(envelope: ToolResultEnvelope) -> ToolResultEnvelope:
            max_latency_ms = int(runtime_max_tool_call_seconds) * 1000
            if envelope.latency_ms <= max_latency_ms:
                return envelope
            return ToolResultEnvelope(
                ok=False,
                tool=envelope.tool,
                call_id=envelope.call_id,
                result=None,
                error=f"Tool '{envelope.tool}' exceeded max_tool_call_seconds={runtime_max_tool_call_seconds}",
                latency_ms=envelope.latency_ms,
            )

        if not _is_tool_allowed(
            tool_name=invocation.name,
            enabled_tools=enabled_tools,
            disabled_tools_policy=disabled_tools_policy,
        ):
            return _bounded_envelope(ToolResultEnvelope(
                ok=False,
                tool=invocation.name,
                call_id=invocation.call_id,
                result=None,
                error=f"Tool '{invocation.name}' is disabled by policy.",
                latency_ms=0,
            ))

        started = time.time()
        bot_id = str(bot["bot_id"])
        try:
            if invocation.source == "mcp":
                tool_name = invocation.name
                if not mcp_enabled_runtime:
                    raise RuntimeError("MCP access is disabled by configuration.")
                if tool_name in mcp_disabled_tools:
                    raise RuntimeError(f"MCP tool '{tool_name}' is disabled by configuration.")
                mcp_result = mcp_broker.call_tool(
                    full_name=tool_name,
                    arguments=invocation.arguments,
                    bot_id=bot_id,
                )
                latency_ms = int((time.time() - started) * 1000)
                return _bounded_envelope(ToolResultEnvelope(
                    ok=True,
                    tool=invocation.name,
                    call_id=invocation.call_id,
                    result={"bot_id": bot_id, "tool": tool_name, "result": mcp_result},
                    error=None,
                    latency_ms=latency_ms,
                ))

            tool_name = invocation.name
            if tool_name == "shell":
                if not shell_enabled_runtime:
                    raise RuntimeError("Shell access is disabled by configuration.")
                command = str(invocation.arguments.get("value", "")).strip()
                shell_result = _run_shell(command)
                return _bounded_envelope(ToolResultEnvelope(
                    ok=bool(shell_result.get("ok", False)),
                    tool=tool_name,
                    call_id=invocation.call_id,
                    result={"bot_id": bot_id, **shell_result},
                    error=str(shell_result.get("error", "")).strip() or None,
                    latency_ms=int((time.time() - started) * 1000),
                ))

            if tool_name == "web_search":
                query = str(invocation.arguments.get("value", "")).strip()
                web_result = _web_search(query)
                return _bounded_envelope(ToolResultEnvelope(
                    ok=True,
                    tool=tool_name,
                    call_id=invocation.call_id,
                    result={"bot_id": bot_id, **web_result},
                    error=None,
                    latency_ms=int((time.time() - started) * 1000),
                ))

            if tool_name == "web_fetch":
                url = str(invocation.arguments.get("value", "")).strip()
                web_result = _web_fetch(url)
                return _bounded_envelope(ToolResultEnvelope(
                    ok=True,
                    tool=tool_name,
                    call_id=invocation.call_id,
                    result={"bot_id": bot_id, **web_result},
                    error=None,
                    latency_ms=int((time.time() - started) * 1000),
                ))

            if tool_name == "gateway":
                gateway_result = _handle_gateway_tool(bot_id=bot_id, call=invocation.arguments)
                return _bounded_envelope(ToolResultEnvelope(
                    ok=bool(gateway_result.get("ok", False)),
                    tool=tool_name,
                    call_id=invocation.call_id,
                    result=gateway_result,
                    error=None,
                    latency_ms=int((time.time() - started) * 1000),
                ))

            if tool_name == "current_time":
                timezone_name = str(invocation.arguments.get("timezone", "UTC")).strip() or "UTC"
                now_iso = datetime.now(timezone.utc).isoformat()
                return _bounded_envelope(ToolResultEnvelope(
                    ok=True,
                    tool=tool_name,
                    call_id=invocation.call_id,
                    result={"timezone": timezone_name, "now_iso": now_iso},
                    error=None,
                    latency_ms=int((time.time() - started) * 1000),
                ))

            if tool_name == "get_callback_url":
                return _bounded_envelope(ToolResultEnvelope(
                    ok=True,
                    tool=tool_name,
                    call_id=invocation.call_id,
                    result={
                        "bot_id": bot_id,
                        "callback_url": _get_callback_url(),
                        "path": "/hooks/outward_inbox",
                    },
                    error=None,
                    latency_ms=int((time.time() - started) * 1000),
                ))

            if tool_name == "send_telegram_messege":
                telegram_result = _send_telegram_tool_message(
                    bot=bot,
                    current_session_id=session_id,
                    message_text=str(invocation.arguments.get("message", "")).strip(),
                )
                return _bounded_envelope(ToolResultEnvelope(
                    ok=bool(telegram_result.get("ok", False)),
                    tool=tool_name,
                    call_id=invocation.call_id,
                    result=telegram_result,
                    error=None,
                    latency_ms=int((time.time() - started) * 1000),
                ))

            raise RuntimeError(f"Unknown tool '{tool_name}'")
        except Exception as exc:
            return _bounded_envelope(ToolResultEnvelope(
                ok=False,
                tool=invocation.name,
                call_id=invocation.call_id,
                result=None,
                error=str(exc),
                latency_ms=int((time.time() - started) * 1000),
            ))

    def _run_contract_turn(
        *,
        bot: Dict[str, Any],
        session_id: str,
        user_message: str,
        selected_model: Optional[str],
        enabled_tools: Optional[List[str]],
        disabled_tools_policy: Optional[List[str]],
    ) -> Dict[str, Any]:
        messages = _build_llm_messages(
            bot=bot,
            session_id=session_id,
            user_message=user_message,
        )

        def _call_model(messages_payload: List[Dict[str, str]]) -> str:
            return _call_llm(messages=messages_payload, selected_model=selected_model, bot=bot)

        outcome = run_turn_loop(
            initial_messages=messages,
            call_model=_call_model,
            parse_tool_call=_resolve_contract_tool_call,
            execute_tool_call=lambda invocation: _execute_contract_tool_call(
                bot=bot,
                session_id=session_id,
                invocation=invocation,
                enabled_tools=enabled_tools,
                disabled_tools_policy=disabled_tools_policy,
            ),
            max_tool_passes=runtime_max_tool_passes,
            max_turn_seconds=runtime_max_turn_seconds,
            max_output_chars=runtime_max_output_chars,
        )
        return {
            "assistant_message": outcome.assistant_message,
            "tool_trace": outcome.tool_trace,
        }

    def _generate_chat_response(
        bot: Dict[str, Any],
        session_id: str,
        user_message: str,
        selected_model: Optional[str],
        on_tool_trace: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        bot_id = str(bot["bot_id"])
        messages = _build_llm_messages(bot=bot, session_id=session_id, user_message=user_message)
        mcp_call_happened = False
        forced_mcp_retry = False
        tool_trace: List[Dict[str, Any]] = []

        def _new_tool_call_id(step: int) -> str:
            return f"tc_{max(1, int(step))}_{uuid.uuid4().hex[:8]}"

        def _emit_tool_trace_start(
            *,
            call_id: str,
            tool: str,
            source: str,
            arguments: Dict[str, Any],
        ) -> None:
            if on_tool_trace is None:
                return
            try:
                on_tool_trace(
                    {
                        "call_id": str(call_id or "").strip() or f"tc_{uuid.uuid4().hex[:8]}",
                        "tool": str(tool or "").strip(),
                        "source": str(source or "").strip() or "builtin",
                        "arguments": arguments if isinstance(arguments, dict) else {},
                        "status": "running",
                        "ok": None,
                        "result": None,
                        "error": None,
                        "latency_ms": 0,
                    }
                )
            except Exception:
                pass

        def _record_tool_trace(
            *,
            call_id: str,
            step: int,
            tool: str,
            source: str,
            arguments: Dict[str, Any],
            ok: bool,
            result: Optional[Dict[str, Any]],
            error: Optional[str],
            started_at: float,
        ) -> None:
            tool_trace.append(
                {
                    "call_id": str(call_id or "").strip() or f"tc_{max(1, int(step))}_{uuid.uuid4().hex[:8]}",
                    "tool": str(tool or "").strip(),
                    "source": str(source or "").strip() or "builtin",
                    "arguments": arguments if isinstance(arguments, dict) else {},
                    "status": "ok" if bool(ok) else "error",
                    "ok": bool(ok),
                    "result": result if isinstance(result, dict) else None,
                    "error": str(error or "").strip() or None,
                    "latency_ms": max(0, int((time.time() - started_at) * 1000)),
                }
            )
            if on_tool_trace is not None:
                try:
                    on_tool_trace(dict(tool_trace[-1]))
                except Exception:
                    pass

        for step in range(1, shell_max_calls_per_turn + 2):
            assistant_text = _call_llm(messages=messages, selected_model=selected_model, bot=bot)
            try:
                mcp_call = _extract_mcp_tool_call(assistant_text)
            except Exception as exc:
                return {"assistant_text": str(exc), "tool_trace": tool_trace}
            if mcp_call is not None:
                tool_name, args = mcp_call
                if not mcp_enabled_runtime:
                    return {"assistant_text": "MCP access is disabled by configuration.", "tool_trace": tool_trace}
                if tool_name in mcp_disabled_tools:
                    return {
                        "assistant_text": f"MCP tool '{tool_name}' is disabled by configuration.",
                        "tool_trace": tool_trace,
                    }
                started = time.time()
                call_id = _new_tool_call_id(step)
                _emit_tool_trace_start(
                    call_id=call_id,
                    tool=tool_name,
                    source="mcp",
                    arguments=args,
                )
                try:
                    mcp_result = mcp_broker.call_tool(full_name=tool_name, arguments=args, bot_id=bot_id)
                    mcp_call_happened = True
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool=tool_name,
                        source="mcp",
                        arguments=args,
                        ok=True,
                        result={"bot_id": bot_id, "tool": tool_name, "result": mcp_result},
                        error=None,
                        started_at=started,
                    )
                except Exception as exc:
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool=tool_name,
                        source="mcp",
                        arguments=args,
                        ok=False,
                        result=None,
                        error=str(exc),
                        started_at=started,
                    )
                    raise
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOOL_RESULT mcp\n"
                            f"{json.dumps({'bot_id': bot_id, 'tool': tool_name, 'result': mcp_result}, ensure_ascii=True)}\n"
                            "Now continue and answer the user directly."
                        ),
                    }
                )
                continue

            web_search_query = _extract_web_search_query(assistant_text)
            if web_search_query:
                started = time.time()
                call_id = _new_tool_call_id(step)
                _emit_tool_trace_start(
                    call_id=call_id,
                    tool="web_search",
                    source="builtin",
                    arguments={"value": web_search_query},
                )
                try:
                    web_result = _web_search(web_search_query)
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="web_search",
                        source="builtin",
                        arguments={"value": web_search_query},
                        ok=True,
                        result={"bot_id": bot_id, **web_result},
                        error=None,
                        started_at=started,
                    )
                except Exception as exc:
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="web_search",
                        source="builtin",
                        arguments={"value": web_search_query},
                        ok=False,
                        result=None,
                        error=str(exc),
                        started_at=started,
                    )
                    raise
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOOL_RESULT web_search\n"
                            f"{json.dumps({'bot_id': bot_id, **web_result}, ensure_ascii=True)}\n"
                            "Now continue and answer the user directly."
                        ),
                    }
                )
                continue

            web_fetch_url = _extract_web_fetch_url(assistant_text)
            if web_fetch_url:
                started = time.time()
                call_id = _new_tool_call_id(step)
                _emit_tool_trace_start(
                    call_id=call_id,
                    tool="web_fetch",
                    source="builtin",
                    arguments={"value": web_fetch_url},
                )
                try:
                    web_result = _web_fetch(web_fetch_url)
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="web_fetch",
                        source="builtin",
                        arguments={"value": web_fetch_url},
                        ok=True,
                        result={"bot_id": bot_id, **web_result},
                        error=None,
                        started_at=started,
                    )
                except Exception as exc:
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="web_fetch",
                        source="builtin",
                        arguments={"value": web_fetch_url},
                        ok=False,
                        result=None,
                        error=str(exc),
                        started_at=started,
                    )
                    raise
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOOL_RESULT web_fetch\n"
                            f"{json.dumps({'bot_id': bot_id, **web_result}, ensure_ascii=True)}\n"
                            "Now continue and answer the user directly."
                        ),
                    }
                )
                continue

            gateway_call = _extract_gateway_tool_call(assistant_text)
            if gateway_call is not None:
                started = time.time()
                call_id = _new_tool_call_id(step)
                gateway_args = gateway_call if isinstance(gateway_call, dict) else {}
                _emit_tool_trace_start(
                    call_id=call_id,
                    tool="gateway",
                    source="gateway",
                    arguments=gateway_args,
                )
                try:
                    gateway_result = _handle_gateway_tool(bot_id=bot_id, call=gateway_call)
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="gateway",
                        source="gateway",
                        arguments=gateway_args,
                        ok=bool(gateway_result.get("ok", False)),
                        result=gateway_result if isinstance(gateway_result, dict) else None,
                        error=None,
                        started_at=started,
                    )
                except Exception as exc:
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="gateway",
                        source="gateway",
                        arguments=gateway_args,
                        ok=False,
                        result=None,
                        error=str(exc),
                        started_at=started,
                    )
                    raise
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOOL_RESULT gateway\n"
                            f"{json.dumps(gateway_result, ensure_ascii=True)}\n"
                            "Now continue and answer the user directly."
                        ),
                    }
                )
                continue

            if _extract_get_callback_url_call(assistant_text):
                started = time.time()
                call_id = _new_tool_call_id(step)
                _emit_tool_trace_start(
                    call_id=call_id,
                    tool="get_callback_url",
                    source="gateway",
                    arguments={},
                )
                callback_result = {
                    "bot_id": bot_id,
                    "callback_url": _get_callback_url(),
                    "path": "/hooks/outward_inbox",
                }
                _record_tool_trace(
                    call_id=call_id,
                    step=step,
                    tool="get_callback_url",
                    source="gateway",
                    arguments={},
                    ok=True,
                    result=callback_result,
                    error=None,
                    started_at=started,
                )
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOOL_RESULT get_callback_url\n"
                            f"{json.dumps(callback_result, ensure_ascii=True)}\n"
                            "Now continue and answer the user directly."
                        ),
                    }
                )
                continue

            telegram_message = _extract_telegram_send_call(assistant_text)
            if telegram_message is not None:
                started = time.time()
                call_id = _new_tool_call_id(step)
                _emit_tool_trace_start(
                    call_id=call_id,
                    tool="send_telegram_messege",
                    source="gateway",
                    arguments={"message": telegram_message},
                )
                try:
                    telegram_result = _send_telegram_tool_message(
                        bot=bot,
                        current_session_id=session_id,
                        message_text=telegram_message,
                    )
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="send_telegram_messege",
                        source="gateway",
                        arguments={"message": telegram_message},
                        ok=bool(telegram_result.get("ok", False)),
                        result=telegram_result if isinstance(telegram_result, dict) else None,
                        error=None,
                        started_at=started,
                    )
                except Exception as exc:
                    _record_tool_trace(
                        call_id=call_id,
                        step=step,
                        tool="send_telegram_messege",
                        source="gateway",
                        arguments={"message": telegram_message},
                        ok=False,
                        result=None,
                        error=str(exc),
                        started_at=started,
                    )
                    raise
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "TOOL_RESULT send_telegram_messege\n"
                            f"{json.dumps(telegram_result, ensure_ascii=True)}\n"
                            "Now continue and answer the user directly."
                        ),
                    }
                )
                continue

            shell_command = _extract_shell_command(assistant_text)
            if not shell_command:
                if (
                    not mcp_call_happened
                    and mcp_enabled_runtime
                    and bool(_get_active_mcp_tools())
                    and _mcp_verification_required(user_message, assistant_text)
                ):
                    if forced_mcp_retry:
                        return {
                            "assistant_text": (
                                "I couldn't verify that in the backend yet. "
                                "I'll run the MCP tool first before claiming completion."
                            ),
                            "tool_trace": tool_trace,
                        }
                    forced_mcp_retry = True
                    messages.append({"role": "assistant", "content": assistant_text})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response claimed backend state without calling an MCP tool. "
                                "For this request, call exactly one MCP tool now using "
                                "<tool:mcp name=\"server.tool\">{\"arg\":\"value\"}</tool:mcp> "
                                "and do not provide a normal answer yet."
                            ),
                        }
                    )
                    continue
                return {"assistant_text": assistant_text, "tool_trace": tool_trace}

            if not shell_enabled_runtime:
                return {"assistant_text": "Shell access is disabled by configuration.", "tool_trace": tool_trace}

            started = time.time()
            call_id = _new_tool_call_id(step)
            _emit_tool_trace_start(
                call_id=call_id,
                tool="shell",
                source="builtin",
                arguments={"value": shell_command},
            )
            try:
                shell_result = _run_shell(shell_command)
                _record_tool_trace(
                    call_id=call_id,
                    step=step,
                    tool="shell",
                    source="builtin",
                    arguments={"value": shell_command},
                    ok=bool(shell_result.get("ok", False)),
                    result={"bot_id": bot_id, **shell_result},
                    error=str(shell_result.get("error", "")).strip() or None,
                    started_at=started,
                )
            except Exception as exc:
                _record_tool_trace(
                    call_id=call_id,
                    step=step,
                    tool="shell",
                    source="builtin",
                    arguments={"value": shell_command},
                    ok=False,
                    result=None,
                    error=str(exc),
                    started_at=started,
                )
                raise
            messages.append({"role": "assistant", "content": assistant_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "TOOL_RESULT shell\n"
                        f"{json.dumps({'bot_id': bot_id, **shell_result}, ensure_ascii=True)}\n"
                        "Now continue and answer the user directly."
                    ),
                }
            )

        return {
            "assistant_text": "I hit the shell tool-call limit for this turn. Please narrow the request and try again.",
            "tool_trace": tool_trace,
        }

    def _process_telegram_message(
        bot_id: str,
        message: Dict[str, Any],
        update_id: Optional[int],
        source_path: str,
        append_user_message: bool = True,
    ) -> Dict[str, Any]:
        bot = _require_bot(bot_id)
        token = str(bot.get("telegram_bot_token", "")).strip()
        if not _telegram_token_ready(token):
            raise RuntimeError("telegram_bot_token is not configured for this BOT_ID")

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

        from_user = message.get("from")
        sender_user_id: Optional[int] = None
        if isinstance(from_user, dict):
            sender_id_raw = from_user.get("id")
            if isinstance(sender_id_raw, (int, str)) and str(sender_id_raw).strip():
                try:
                    sender_user_id = int(sender_id_raw)
                except ValueError:
                    sender_user_id = None
        if sender_user_id is None:
            return {"status": "ignored", "reason": "missing sender id"}

        existing_owner = bot.get("telegram_owner_user_id")
        if existing_owner is None:
            bot = state.session_store.set_telegram_owner(bot_id=bot_id, user_id=sender_user_id, chat_id=chat_id)
        elif int(existing_owner) != int(sender_user_id):
            denial = "This bot is privately owned and cannot be used by this Telegram account."
            try:
                _telegram_send_message(token, chat_id, denial, None)
            except Exception:
                pass
            state.record_event(
                {
                    "received_at": time.time(),
                    "path": source_path,
                    "bot_id": bot_id,
                    "payload": {
                        "event_type": "telegram_owner_mismatch",
                        "telegram_chat_id": chat_id,
                        "sender_user_id": sender_user_id,
                    },
                    "error": "telegram owner mismatch",
                }
            )
            return {"status": "forbidden", "reason": "telegram owner mismatch", "bot_id": bot_id}

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
        if append_user_message:
            state.append_session(bot_id=bot_id, session_id=session_id, role="user", content=user_message)
        else:
            _append_user_message_if_missing(bot_id=bot_id, session_id=session_id, user_message=user_message)
        chat_payload = {
            "source": "telegram",
            "bot_id": bot_id,
            "event_type": "telegram_message",
            "session_id": session_id,
            "note": user_message,
            "task_description": "Telegram chat message",
            "task_status": "active",
            "task_done": False,
            "telegram_update_id": update_id,
            "telegram_chat_id": chat_id,
            "telegram_message_id": reply_to_message_id,
            "sender_user_id": sender_user_id,
        }
        event_record = {
            "received_at": time.time(),
            "path": source_path,
            "bot_id": bot_id,
            "session_id": session_id,
            "payload": chat_payload,
            "rendered_message": user_message,
            "forwarded": False,
            "telegram_sent": False,
        }

        selected_model = str(bot.get("model", "")).strip() or default_model
        chat_result = _generate_chat_response(
            bot=bot,
            session_id=session_id,
            user_message=user_message,
            selected_model=selected_model,
        )
        assistant_text = str(chat_result.get("assistant_text", "")).strip()
        tool_trace = chat_result.get("tool_trace") if isinstance(chat_result.get("tool_trace"), list) else []
        state.append_session(bot_id=bot_id, session_id=session_id, role="assistant", content=assistant_text)
        forward_result = _forward(
            bot_id=bot_id,
            message=assistant_text,
            payload={**chat_payload, "assistant_response": assistant_text},
        )
        telegram_delivery = _dispatch_outbound_event(
            bot_id=bot_id,
            channel="telegram_send",
            payload={
                "bot_token": token,
                "chat_id": chat_id,
                "text": assistant_text,
                "reply_to_message_id": reply_to_message_id,
            },
            idempotency_key=f"tgreply:{bot_id}:{chat_id}:{reply_to_message_id}:{update_id}",
        )
        event_record["forwarded"] = bool(forward_result.get("ok"))
        event_record["telegram_sent"] = bool(telegram_delivery.get("ok"))
        event_record["assistant_response"] = assistant_text
        event_record["tool_trace"] = tool_trace
        state.record_event(event_record)
        return {
            "status": "ok",
            "bot_id": bot_id,
            "session_id": session_id,
            "response": assistant_text,
            "tool_trace": tool_trace,
            "forwarded": bool(forward_result.get("ok")),
            "telegram_sent": bool(telegram_delivery.get("ok")),
            "telegram_delivery": telegram_delivery,
        }

    def _execute_chat_event_payload(
        payload: Dict[str, Any],
        source_path: str,
        append_user_message: bool = True,
    ) -> Dict[str, Any]:
        bot_id = _safe_bot_id(str(payload.get("bot_id", "")).strip())
        if not bot_id:
            raise RuntimeError("bot_id is required")
        bot = _require_bot(bot_id)
        session_id = str(payload.get("session_id", "")).strip() or "default"
        user_message = str(payload.get("message", "")).strip()
        if not user_message:
            raise RuntimeError("message is required")

        selected_model = (
            str(payload.get("model", "")).strip()
            or str(bot.get("model", "")).strip()
            or default_model
        )

        if append_user_message:
            state.append_session(bot_id=bot_id, session_id=session_id, role="user", content=user_message)
        else:
            _append_user_message_if_missing(bot_id=bot_id, session_id=session_id, user_message=user_message)
        chat_payload = {
            "source": str(payload.get("source", "chat-ui")).strip() or "chat-ui",
            "bot_id": bot_id,
            "event_type": "chat_message",
            "session_id": session_id,
            "note": user_message,
            "task_description": "User chat message",
            "task_status": "active",
            "task_done": False,
            "model": selected_model,
        }
        turn_id = f"turn_{uuid.uuid4().hex}"
        event_record = {
            "received_at": time.time(),
            "path": source_path,
            "event_type": "chat_message",
            "bot_id": bot_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "payload": chat_payload,
            "rendered_message": user_message,
            "forwarded": False,
        }
        try:
            progress_seq = 0

            def _on_tool_trace(entry: Dict[str, Any]) -> None:
                nonlocal progress_seq
                progress_seq += 1
                progress_event = {
                    "received_at": time.time(),
                    "path": source_path,
                    "event_type": "tool_call_progress",
                    "bot_id": bot_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "progress_seq": progress_seq,
                    "tool_trace": [entry] if isinstance(entry, dict) else [],
                }
                state.record_event(progress_event)
                tool_event_stream_broker.publish(bot_id=bot_id, event=progress_event)

            chat_result = _generate_chat_response(
                bot=bot,
                session_id=session_id,
                user_message=user_message,
                selected_model=selected_model,
                on_tool_trace=_on_tool_trace,
            )
            assistant_text = str(chat_result.get("assistant_text", "")).strip()
            tool_trace = chat_result.get("tool_trace") if isinstance(chat_result.get("tool_trace"), list) else []
            state.append_session(bot_id=bot_id, session_id=session_id, role="assistant", content=assistant_text)
            forward_result = _forward(
                bot_id=bot_id,
                message=assistant_text,
                payload={**chat_payload, "assistant_response": assistant_text},
            )
            event_record["forwarded"] = bool(forward_result.get("ok"))
            event_record["assistant_response"] = assistant_text
            event_record["tool_trace"] = tool_trace
            state.record_event(event_record)
            return {
                "status": "ok",
                "bot_id": bot_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "model": selected_model,
                "response": assistant_text,
                "tool_trace": tool_trace,
                "forwarded": bool(forward_result.get("ok")),
                "forward_result": forward_result,
            }
        except Exception as exc:
            state.record_event({**event_record, "error": str(exc)})
            raise

    def _enqueue_chat_event(
        *,
        bot_id: str,
        session_id: str,
        user_message: str,
        selected_model: str,
        idempotency_key: Optional[str] = None,
        source: str = "chat-ui",
        source_path: str = "/api/chat",
    ) -> Dict[str, Any]:
        payload = {
            "bot_id": bot_id,
            "session_id": session_id,
            "message": user_message,
            "model": selected_model,
            "source": str(source or "chat-ui").strip() or "chat-ui",
            "source_path": str(source_path or "/api/chat").strip() or "/api/chat",
        }
        idem = str(idempotency_key or f"chat:{bot_id}:{session_id}:{uuid.uuid4().hex}").strip()
        return state.session_store.enqueue_inbound_event(
            bot_id=bot_id,
            session_id=session_id,
            source="gateway",
            event_type="chat_message",
            payload=payload,
            idempotency_key=idem,
        )

    def _hook_part(value: Any, fallback: str = "unknown") -> str:
        clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
        return clean or fallback

    def _build_hook_session_id(
        *,
        hook_name: str,
        primary: str,
        payload: Dict[str, Any],
        secondary: str = "",
    ) -> str:
        explicit = str(payload.get("session_id", "")).strip()
        if explicit:
            return explicit
        event_hint = (
            str(payload.get("event_id", "")).strip()
            or str(payload.get("id", "")).strip()
            or str(payload.get("idempotency_key", "")).strip()
            or uuid.uuid4().hex[:12]
        )
        parts = [
            "hook",
            _hook_part(hook_name, "hook"),
            _hook_part(primary, "unknown"),
        ]
        secondary_part = _hook_part(secondary, "")
        if secondary_part:
            parts.append(secondary_part)
        parts.append(_hook_part(event_hint, uuid.uuid4().hex[:12]))
        return ":".join(parts)

    def _enqueue_telegram_event(
        *,
        bot_id: str,
        message: Dict[str, Any],
        update_id: Optional[int],
        source_path: str,
    ) -> Dict[str, Any]:
        chat = message.get("chat") if isinstance(message, dict) else {}
        chat_id_raw = chat.get("id") if isinstance(chat, dict) else None
        session_id = "telegram:unknown"
        if isinstance(chat_id_raw, (int, str)) and str(chat_id_raw).strip():
            try:
                session_id = f"telegram:{int(chat_id_raw)}"
            except ValueError:
                session_id = "telegram:unknown"
        idem_suffix = str(update_id) if update_id is not None else uuid.uuid4().hex
        idem = f"tg:{bot_id}:{idem_suffix}"
        payload = {
            "bot_id": bot_id,
            "message": message if isinstance(message, dict) else {},
            "update_id": update_id,
            "source_path": source_path,
        }
        return state.session_store.enqueue_inbound_event(
            bot_id=bot_id,
            session_id=session_id,
            source="telegram",
            event_type="telegram_message",
            payload=payload,
            idempotency_key=idem,
        )

    def _execute_inbound_event(event: Dict[str, Any]) -> Dict[str, Any]:
        status = str(event.get("status", "")).strip().lower()
        if status == "done":
            cached = event.get("result")
            return cached if isinstance(cached, dict) else {"status": "ok"}
        if status in {"error", "dead_letter"}:
            raise RuntimeError(str(event.get("error", "inbound event failed")).strip() or "inbound event failed")
        if status == "processing":
            event_id_for_refresh = str(event.get("event_id", "")).strip()
            if event_id_for_refresh:
                refreshed = state.session_store.get_inbound_event(event_id_for_refresh)
                if isinstance(refreshed, dict):
                    refreshed_status = str(refreshed.get("status", "")).strip().lower()
                    if refreshed_status == "done":
                        cached = refreshed.get("result")
                        return cached if isinstance(cached, dict) else {"status": "ok"}
                    if refreshed_status in {"error", "dead_letter"}:
                        raise RuntimeError(str(refreshed.get("error", "inbound event failed")).strip() or "inbound event failed")

        event_type = str(event.get("event_type", "")).strip()
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        event_id = str(event.get("event_id", "")).strip()
        attempt_number = max(1, int(event.get("attempts") or 1))
        append_user_message = attempt_number <= 1
        try:
            if event_type == "chat_message":
                source_path = str(payload.get("source_path", "/api/chat")).strip() or "/api/chat"
                result = _execute_chat_event_payload(
                    payload=payload,
                    source_path=source_path,
                    append_user_message=append_user_message,
                )
            elif event_type == "telegram_message":
                bot_id = _safe_bot_id(str(payload.get("bot_id", "")).strip())
                if not bot_id:
                    raise RuntimeError("telegram_message event missing bot_id")
                message = payload.get("message")
                if not isinstance(message, dict):
                    raise RuntimeError("telegram_message event missing message object")
                update_id = payload.get("update_id")
                parsed_update_id: Optional[int] = None
                if isinstance(update_id, (int, str)) and str(update_id).strip():
                    try:
                        parsed_update_id = int(update_id)
                    except ValueError:
                        parsed_update_id = None
                source_path = str(payload.get("source_path", "/api/telegram/inbound")).strip() or "/api/telegram/inbound"
                result = _process_telegram_message(
                    bot_id=bot_id,
                    message=message,
                    update_id=parsed_update_id,
                    source_path=source_path,
                    append_user_message=append_user_message,
                )
            else:
                raise RuntimeError(f"Unsupported inbound event_type: {event_type}")
            if event_id:
                state.session_store.complete_inbound_event(event_id, result if isinstance(result, dict) else {"status": "ok"})
            return result if isinstance(result, dict) else {"status": "ok"}
        except Exception as exc:
            if event_id:
                state.session_store.fail_inbound_event(
                    event_id,
                    str(exc),
                    max_attempts=queue_max_attempts,
                    retry_base_s=queue_retry_base_s,
                    retry_cap_s=queue_retry_cap_s,
                )
            raise

    def _wait_for_inbound_result(event_id: str, timeout_s: int) -> Optional[Dict[str, Any]]:
        deadline = time.time() + max(1, int(timeout_s))
        while time.time() < deadline:
            row = state.session_store.get_inbound_event(event_id)
            if not row:
                return None
            status = str(row.get("status", "")).strip()
            if status == "done":
                result = row.get("result")
                return result if isinstance(result, dict) else {"status": "ok"}
            if status in {"error", "dead_letter"}:
                raise RuntimeError(str(row.get("error", "inbound event failed")).strip() or "inbound event failed")
            time.sleep(0.05)
        return None

    def _run_executor_once(worker_id: str) -> bool:
        event = state.session_store.claim_next_inbound_event(worker_id=worker_id, lock_timeout_s=queue_lock_timeout_s)
        if not event:
            return False
        _execute_inbound_event(event)
        return True

    def _run_delivery_once(worker_id: str) -> bool:
        outbound_event = state.session_store.claim_next_outbound_event(worker_id=worker_id, lock_timeout_s=queue_lock_timeout_s)
        if not outbound_event:
            return False
        event_id = str(outbound_event.get("event_id", "")).strip()
        try:
            result = _deliver_outbound_event(outbound_event)
            state.session_store.complete_outbound_event(event_id, result if isinstance(result, dict) else {"status": "ok"})
        except Exception as exc:
            state.session_store.fail_outbound_event(
                event_id,
                str(exc),
                max_attempts=queue_max_attempts,
                retry_base_s=queue_retry_base_s,
                retry_cap_s=queue_retry_cap_s,
            )
            raise
        return True

    def _run_executor_loop(worker_id: str) -> None:
        while True:
            try:
                did_work = _run_executor_once(worker_id)
                if not did_work:
                    time.sleep(queue_poll_interval_s)
            except Exception as exc:
                state.record_event(
                    {
                        "received_at": time.time(),
                        "path": "/workers/executor",
                        "error": str(exc),
                        "payload": {"worker_id": worker_id},
                    }
                )
                time.sleep(queue_poll_interval_s)

    def _run_delivery_loop(worker_id: str) -> None:
        while True:
            try:
                did_work = _run_delivery_once(worker_id)
                if not did_work:
                    time.sleep(queue_poll_interval_s)
            except Exception as exc:
                state.record_event(
                    {
                        "received_at": time.time(),
                        "path": "/workers/delivery",
                        "error": str(exc),
                        "payload": {"worker_id": worker_id},
                    }
                )
                time.sleep(queue_poll_interval_s)

    def _run_telegram_poller_loop(worker_id: str) -> None:
        while True:
            if not gateway_plane_enabled or not telegram_poller_enabled:
                time.sleep(queue_poll_interval_s)
                continue
            bots = state.session_store.list_bots(limit=1000)
            did_work = False
            for bot in bots:
                bot_id = str(bot.get("bot_id", "")).strip()
                token = str(bot.get("telegram_bot_token", "")).strip()
                if not bot_id or not _telegram_token_ready(token):
                    continue
                offset = state.session_store.get_telegram_offset(bot_id)
                try:
                    updates = _telegram_get_updates(bot_token=token, offset=offset, timeout_s=telegram_poll_timeout_s)
                    next_offset = offset
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
                            next_offset = max(next_offset, update_id + 1)
                        message = update.get("message")
                        if not isinstance(message, dict):
                            continue
                        _enqueue_telegram_event(
                            bot_id=bot_id,
                            message=message,
                            update_id=update_id,
                            source_path="/api/telegram/poll",
                        )
                        did_work = True
                    if next_offset != offset:
                        state.session_store.set_telegram_offset(bot_id, next_offset)
                except Exception as exc:
                    state.record_event(
                        {
                            "received_at": time.time(),
                            "path": "/workers/telegram-poller",
                            "bot_id": bot_id,
                            "error": str(exc),
                            "payload": {"worker_id": worker_id},
                        }
                    )
                    time.sleep(float(telegram_poll_retry_s))
            if not did_work:
                time.sleep(queue_poll_interval_s)

    def _require_bot_id_from_request(data: Dict[str, Any], required: bool = True) -> str:
        bot_id = str(data.get("bot_id", "")).strip() or str(request.args.get("bot_id", "")).strip()
        clean_bot_id = _safe_bot_id(bot_id)
        if required and not clean_bot_id:
            raise RuntimeError("bot_id is required")
        return clean_bot_id

    def _ensure_gateway_plane() -> None:
        if not gateway_plane_enabled:
            raise RuntimeError("gateway plane is disabled in this service mode")

    def _ensure_executor_plane() -> None:
        if not executor_plane_enabled:
            raise RuntimeError("executor plane is disabled in this service mode")

    @app.route("/", methods=["GET"])
    def index():
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SimpleAgent Chat</title>
  <style>
    :root {
      --bg: #f0f5f6;
      --surface: #ffffff;
      --surface-soft: #f6fbfc;
      --ink: #112431;
      --ink-soft: #566b78;
      --line: #cfe0e8;
      --brand: #0f7f92;
      --brand-strong: #0b6676;
      --danger: #a33535;
      --success: #117a52;
      --shadow: 0 14px 40px rgba(9, 58, 71, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: "IBM Plex Sans", "Avenir Next", "Trebuchet MS", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(15, 127, 146, 0.2), rgba(15, 127, 146, 0) 36%),
        radial-gradient(circle at bottom left, rgba(14, 143, 104, 0.16), rgba(14, 143, 104, 0) 42%),
        var(--bg);
      min-height: 100vh;
    }
    .wrap {
      max-width: 1220px;
      margin: 0 auto;
      padding: 24px 16px 34px;
    }
    .header {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
    }
    h1 {
      margin: 0 0 4px;
      font-size: 28px;
      letter-spacing: -0.02em;
    }
    .muted {
      margin: 0;
      color: var(--ink-soft);
      font-size: 14px;
    }
    .status {
      margin: 0;
      padding: 8px 11px;
      border: 1px solid #bbe2ec;
      border-radius: 999px;
      color: #115f6e;
      background: #e8f8fc;
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
    }
    .chip-row {
      display: flex;
      gap: 8px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      border: 1px solid #c6dbe3;
      background: #f7fbfc;
      color: #2f4957;
      padding: 3px 10px;
      font-size: 11px;
      font-weight: 600;
    }
    .tab-strip {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }
    .tab-btn {
      border: 1px solid #bed7df;
      background: #f7fbfc;
      color: #2c4a57;
      border-radius: 999px;
      padding: 8px 13px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 120ms ease;
    }
    .tab-btn.active {
      border-color: var(--brand);
      background: var(--brand);
      color: #fff;
      box-shadow: 0 8px 20px rgba(15, 127, 146, 0.25);
    }
    .tab-panel {
      display: none;
      animation: panel-in 160ms ease;
    }
    .tab-panel.active { display: block; }
    @keyframes panel-in {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
      padding: 12px;
    }
    .section-title {
      margin: 0 0 8px;
      color: #32515e;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.07em;
    }
    .row {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
      align-items: center;
    }
    .row.wrap { flex-wrap: wrap; }
    .col {
      display: flex;
      flex-direction: column;
      gap: 4px;
      flex: 1;
      min-width: 0;
    }
    .label {
      color: #496372;
      font-size: 12px;
      font-weight: 600;
    }
    input[type='text'],
    input[type='password'],
    textarea,
    select {
      width: 100%;
      border: 1px solid #c2d8e0;
      border-radius: 10px;
      background: #fcfeff;
      color: var(--ink);
      padding: 9px 10px;
      font-size: 13px;
      outline: none;
    }
    input[type='checkbox'] {
      width: 16px;
      height: 16px;
      accent-color: var(--brand);
    }
    textarea {
      min-height: 170px;
      resize: vertical;
      font-family: "IBM Plex Mono", "Menlo", "Consolas", monospace;
    }
    button {
      border: 1px solid var(--brand);
      border-radius: 10px;
      background: var(--brand);
      color: #fff;
      padding: 8px 11px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 130ms ease;
    }
    button:hover { background: var(--brand-strong); border-color: var(--brand-strong); }
    .btn-secondary {
      background: #edf5f8;
      color: #265162;
      border-color: #bdd6e0;
    }
    .btn-secondary:hover {
      background: #dcecf2;
      border-color: #aac8d4;
    }
    .top-bot-bar {
      margin-bottom: 14px;
      padding: 10px 12px;
    }
    .bot-toolbar {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }
    .bot-select-col {
      flex: 1;
      min-width: 250px;
      max-width: 520px;
    }
    .create-dropdown {
      position: relative;
      min-width: 240px;
    }
    .create-dropdown summary {
      list-style: none;
      border: 1px solid #bed7df;
      border-radius: 10px;
      background: #f7fbfc;
      color: #2c4a57;
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .create-dropdown summary::-webkit-details-marker { display: none; }
    .create-dropdown summary::after {
      content: "▾";
      margin-left: auto;
      color: #5a7481;
      transition: transform 120ms ease;
    }
    .create-dropdown[open] summary::after { transform: rotate(180deg); }
    .create-dropdown[open] summary {
      border-color: var(--brand);
      box-shadow: 0 7px 18px rgba(15, 127, 146, 0.16);
      background: #f0fafc;
    }
    .create-dropdown-menu {
      position: absolute;
      top: calc(100% + 6px);
      right: 0;
      z-index: 15;
      width: min(360px, 94vw);
      border: 1px solid #c8dde5;
      border-radius: 12px;
      background: #ffffff;
      box-shadow: 0 16px 40px rgba(8, 48, 61, 0.14);
      padding: 10px;
      display: grid;
      gap: 8px;
    }
    .create-dropdown-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
    }
    .chat-layout {
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 12px;
    }
    .chat-layout.chat-layout-single {
      grid-template-columns: 1fr;
    }
    .sidebar {
      display: flex;
      flex-direction: column;
      min-height: 620px;
    }
    .chat-main {
      display: flex;
      flex-direction: column;
      min-height: 620px;
    }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      overflow-y: auto;
      max-height: 230px;
    }
    #sessionList.list { max-height: 520px; }
    .item {
      border: 1px solid #ccdde3;
      border-radius: 10px;
      background: #f9fcfd;
      color: #1f3a46;
      text-align: left;
      padding: 8px 10px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }
    .item.active {
      border-color: #9ecdd8;
      background: #e9f7fb;
      color: #124a5a;
    }
    .chat-top {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .chat {
      flex: 1;
      min-height: 360px;
      max-height: 490px;
      overflow-y: auto;
      border: 1px solid #c8dce4;
      border-radius: 12px;
      background: var(--surface-soft);
      padding: 11px;
      margin-bottom: 10px;
    }
    .msg {
      margin-bottom: 8px;
      padding: 8px 10px;
      border-radius: 10px;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.35;
      font-size: 13px;
    }
    .msg.user {
      background: #e4f4fd;
      border: 1px solid #bdddf0;
      color: #164d64;
    }
    .msg.assistant {
      background: #eef8ef;
      border: 1px solid #cbe4cd;
      color: #1b4f2e;
    }
    .msg.system {
      background: #f4f5f7;
      border: 1px solid #dde3e7;
      color: #485862;
    }
    .msg.error {
      background: #fbeeed;
      border: 1px solid #efc4c0;
      color: #8c2626;
    }
    .tool-trace-group {
      margin-bottom: 10px;
      padding: 8px;
      border-radius: 11px;
      border: 1px solid #c8d9e0;
      background: #f9fcfd;
    }
    .tool-trace-title {
      font-size: 11px;
      font-weight: 700;
      color: #42616e;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 6px;
    }
    .tool-call {
      border: 1px solid #d4e2e8;
      border-radius: 9px;
      background: #ffffff;
      margin-bottom: 6px;
      overflow: hidden;
    }
    .tool-call summary {
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      list-style: none;
      padding: 7px 9px;
      font-size: 12px;
    }
    .tool-call summary::-webkit-details-marker { display: none; }
    .tool-call summary::after {
      content: "▸";
      margin-left: auto;
      color: #68808b;
      font-size: 12px;
      transition: transform 130ms ease;
    }
    .tool-call[open] summary::after { transform: rotate(90deg); }
    .tool-call-state {
      font-size: 10px;
      font-weight: 700;
      border-radius: 999px;
      padding: 2px 7px;
      border: 1px solid #b6ced8;
      background: #eef5f8;
      color: #315461;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .tool-call-state.ok {
      border-color: #b8dbc7;
      background: #edf8f0;
      color: #1f6a3d;
    }
    .tool-call-state.error {
      border-color: #e3c3c1;
      background: #fbefef;
      color: #8b3030;
    }
    .tool-call-state.running {
      border-color: #c6d3ea;
      background: #eef3fb;
      color: #244b86;
    }
    .tool-call-state.running::before {
      content: "";
      width: 9px;
      height: 9px;
      border-radius: 50%;
      border: 2px solid #7fa3da;
      border-top-color: transparent;
      animation: toolspin 0.8s linear infinite;
      flex: 0 0 auto;
    }
    @keyframes toolspin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    .tool-call-name {
      font-weight: 700;
      color: #244754;
      word-break: break-word;
    }
    .tool-call-meta {
      font-size: 11px;
      color: #5f7784;
      margin-left: auto;
      padding-right: 8px;
    }
    .tool-call-payload {
      margin: 0;
      padding: 8px 10px;
      border-top: 1px solid #e0ebef;
      background: #f8fcfd;
      color: #2a4956;
      font-family: "IBM Plex Mono", "Menlo", "Consolas", monospace;
      font-size: 11px;
      line-height: 1.35;
      overflow-x: auto;
      white-space: pre;
    }
    .compose-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .context-meter-wrap {
      margin-top: 7px;
      display: grid;
      gap: 4px;
    }
    .context-meter {
      width: 100%;
      height: 4px;
      border-radius: 999px;
      background: #dce8ed;
      overflow: hidden;
    }
    .context-meter-bar {
      width: 0%;
      height: 100%;
      border-radius: 999px;
      background: #6a8693;
      transition: width 130ms ease;
    }
    .context-meter-label {
      font-size: 11px;
      color: #647d8a;
      text-align: right;
      font-family: "IBM Plex Mono", "Menlo", "Consolas", monospace;
    }
    .model-row {
      display: flex;
      align-items: center;
      gap: 7px;
    }
    .model-row .label { white-space: nowrap; }
    .model-row select {
      min-width: 220px;
      max-width: 280px;
    }
    .settings-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .memory-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .tool-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }
    .tool-item {
      border: 1px solid #d3e1e7;
      border-radius: 10px;
      padding: 8px 10px;
      background: #fbfdfe;
      display: flex;
      gap: 8px;
      align-items: flex-start;
    }
    .tool-name {
      font-size: 13px;
      font-weight: 700;
      color: #1f3f4c;
      margin-bottom: 2px;
    }
    .tool-desc {
      font-size: 12px;
      color: #516977;
      line-height: 1.3;
    }
    .events-log {
      width: 100%;
      min-height: 420px;
      max-height: 620px;
      overflow: auto;
      margin: 0;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid #cadce4;
      background: #f8fcfd;
      color: #16313b;
      font-family: "IBM Plex Mono", "Menlo", "Consolas", monospace;
      font-size: 12px;
      line-height: 1.4;
    }
    .meta {
      margin-top: 10px;
      color: #58707d;
      font-size: 12px;
    }
    .empty {
      border: 1px dashed #c9d9df;
      border-radius: 10px;
      padding: 10px;
      background: #f9fcfd;
      color: #5d7684;
      font-size: 12px;
    }
    @media (max-width: 1040px) {
      .chat-layout { grid-template-columns: 1fr; }
      .sidebar,
      .chat-main { min-height: 0; }
      .list { max-height: 180px; }
      #sessionList.list { max-height: 280px; }
      .bot-select-col,
      .create-dropdown {
        min-width: 0;
        width: 100%;
        max-width: none;
      }
      .create-dropdown-menu {
        position: static;
        width: 100%;
        box-shadow: none;
        border-style: dashed;
        margin-top: 8px;
      }
      .settings-grid,
      .memory-grid,
      .tool-list { grid-template-columns: 1fr; }
      .model-row select { width: 100%; max-width: none; }
      .compose-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="header">
      <div>
        <h1>SimpleAgent Chat</h1>
        <p class="muted">Organized UI for <code>/api/chat</code> with tabs for settings, memory, tools, and events.</p>
      </div>
      <p id="status" class="status">Loading...</p>
    </header>

    <div id="chipRow" class="chip-row">
      <span class="chip" id="activeBotChip">No BOT_ID selected</span>
      <span class="chip" id="activeSessionChip">No session</span>
    </div>

    <section id="topBotBar" class="card top-bot-bar">
      <div class="bot-toolbar">
        <div class="col bot-select-col">
          <label for="botSelect" class="label">BOT_ID</label>
          <select id="botSelect">
            <option value="">No BOT_IDs yet</option>
          </select>
        </div>
        <details id="createBotDropdown" class="create-dropdown">
          <summary>Create Bot</summary>
          <div class="create-dropdown-menu">
            <input id="newBotName" type="text" placeholder="Bot name (optional)" />
            <input id="newBotId" type="text" placeholder="bot_id (optional)" />
            <div class="create-dropdown-actions">
              <button id="createBotBtn" type="button">Create</button>
            </div>
          </div>
        </details>
      </div>
    </section>

    <nav class="tab-strip" role="tablist" aria-label="SimpleAgent sections">
      <button type="button" class="tab-btn active" data-tab-target="chat">Chat</button>
      <button type="button" class="tab-btn" data-tab-target="settings">Settings</button>
      <button type="button" class="tab-btn" data-tab-target="memory">Memory</button>
      <button type="button" class="tab-btn" data-tab-target="tools">Tools</button>
      <button type="button" class="tab-btn" data-tab-target="events">Events</button>
    </nav>

    <section class="tab-panel active" data-tab-panel="chat">
      <div id="chatLayout" class="chat-layout">
        <aside id="sessionSidebar" class="card sidebar">
          <div class="panel-head">
            <h2 class="section-title" style="margin: 0;">Sessions</h2>
            <div class="row" style="margin: 0;">
              <button id="newSessionBtn" type="button" class="btn-secondary">New</button>
              <button id="refreshSessionsBtn" type="button" class="btn-secondary">Refresh</button>
            </div>
          </div>
          <div id="sessionList" class="list"></div>
        </aside>

        <main class="card chat-main">
          <div class="chat-top">
            <p class="section-title" style="margin: 0;">Conversation</p>
            <div class="model-row">
              <label for="modelSelect" class="label">Model</label>
              <select id="modelSelect"></select>
            </div>
          </div>
          <div class="chat" id="chat"></div>
          <form id="chatForm" class="compose-row">
            <input id="msgInput" type="text" placeholder="Type a message" required />
            <button type="submit">Send</button>
          </form>
          <div class="context-meter-wrap">
            <div class="context-meter"><div id="contextMeterBar" class="context-meter-bar"></div></div>
            <div id="contextMeterLabel" class="context-meter-label">context: -- / --</div>
          </div>
        </main>
      </div>
    </section>

    <section class="tab-panel" data-tab-panel="settings">
      <div class="settings-grid">
        <div class="card">
          <h2 class="section-title">Model API Keys</h2>
          <div class="row wrap" id="providerKeyState">
            <span class="chip" id="chipOpenAI">OpenAI: not set</span>
            <span class="chip" id="chipAnthropic">Anthropic: not set</span>
            <span class="chip" id="chipGoogle">Google: not set</span>
          </div>
          <div class="row">
            <input id="openaiApiKeyInput" type="password" placeholder="OpenAI API key (sk-...)" />
          </div>
          <div class="row">
            <input id="anthropicApiKeyInput" type="password" placeholder="Anthropic API key (sk-ant-...)" />
          </div>
          <div class="row">
            <input id="googleApiKeyInput" type="password" placeholder="Google API key (AIza...)" />
          </div>
          <div class="row">
            <button id="saveApiKeysBtn" type="button" class="btn-secondary">Save API Keys</button>
          </div>
        </div>

        <div class="card">
          <h2 class="section-title">Selected Bot Config</h2>
          <div class="row">
            <input id="botNameInput" type="text" placeholder="Bot name" />
            <input id="botHeartbeatInput" type="text" placeholder="Heartbeat interval seconds" />
          </div>
          <div class="row">
            <select id="botModelInput"></select>
            <input id="botTelegramTokenInput" type="password" placeholder="Telegram bot token (optional)" />
          </div>
          <div class="row">
            <button id="saveBotConfigBtn" type="button">Save Bot Config</button>
          </div>
          <p class="meta">BOT_ID is attached to chat and tool flows. Telegram owner is permanently assigned on first incoming Telegram message for each bot token.</p>
        </div>
      </div>
    </section>

    <section class="tab-panel" data-tab-panel="memory">
      <div class="memory-grid">
        <div class="card">
          <h2 class="section-title">SOUL.md</h2>
          <textarea id="soulInput"></textarea>
          <div class="row" style="margin-top: 8px;">
            <button id="saveSoulBtn" type="button" class="btn-secondary">Save SOUL.md</button>
          </div>
        </div>
        <div class="card">
          <h2 class="section-title">USER.md</h2>
          <textarea id="userInput"></textarea>
          <div class="row" style="margin-top: 8px;">
            <button id="saveUserBtn" type="button" class="btn-secondary">Save USER.md</button>
          </div>
        </div>
        <div class="card">
          <h2 class="section-title">HEARTBEAT.md</h2>
          <textarea id="heartbeatInput"></textarea>
          <div class="row" style="margin-top: 8px;">
            <button id="saveHeartbeatBtn" type="button" class="btn-secondary">Save HEARTBEAT.md</button>
          </div>
        </div>
      </div>
    </section>

    <section class="tab-panel" data-tab-panel="tools">
      <div class="card">
        <h2 class="section-title">Runtime Tool Controls</h2>
        <div class="row wrap">
          <label class="row" style="margin: 0;"><input id="shellEnabledInput" type="checkbox" /> <span class="label">Shell enabled</span></label>
          <label class="row" style="margin: 0;"><input id="webEnabledInput" type="checkbox" /> <span class="label">Web enabled</span></label>
          <label class="row" style="margin: 0;"><input id="mcpEnabledInput" type="checkbox" /> <span class="label">MCP enabled</span></label>
        </div>
        <div class="row">
          <button id="saveToolsBtn" type="button">Save Tool Settings</button>
          <button id="refreshToolsBtn" type="button" class="btn-secondary">Refresh</button>
        </div>
        <div id="toolList" class="tool-list"></div>
      </div>
    </section>

    <section class="tab-panel" data-tab-panel="events">
      <div class="card">
        <h2 class="section-title">Debug Event Log</h2>
        <div class="row wrap">
          <button id="refreshEventsBtn" type="button">Refresh Events</button>
          <label class="row" style="margin: 0;"><input id="eventsScopedToBot" type="checkbox" checked /> <span class="label">Only selected BOT_ID</span></label>
          <label class="row" style="margin: 0;"><input id="eventsAutoRefresh" type="checkbox" /> <span class="label">Auto refresh (5s)</span></label>
        </div>
        <pre id="eventsLog" class="events-log">Loading events...</pre>
      </div>
    </section>
  </div>

  <script>
    const MODEL_CATALOG = [
      "gpt-5.3-codex",
      "gpt-5-thinking-high",
      "gpt-5-mini",
      "gpt-5.2-pro",
      "claude-opus-4-6",
      "claude-sonnet-4-6",
      "claude-haiku-4-5",
      "gemini-3.1-pro",
      "gemini-3-flash"
    ];
    const PROVIDER_BY_PREFIX = {
      "gpt-": "openai",
      "claude-": "anthropic",
      "gemini-": "google"
    };

    let selectedBotId = "";
    let selectedSessionId = "";
    let providerKeyState = { openai: false, anthropic: false, google: false };
    let eventsRefreshTimer = null;
    let contextUsageRefreshTimer = null;
    let contextUsageRequestSeq = 0;
    let sessionsRequestSeq = 0;
    let sessionHistoryRequestSeq = 0;
    const pendingSessionIds = new Set();
    let activeToolStream = null;
    const toolCallNodesByKey = new Map();
    const urlParams = new URLSearchParams(window.location.search);
    const uiMode = String(urlParams.get("ui_mode") || "").trim().toLowerCase();

    function isTruthyParam(name) {
      const value = String(urlParams.get(name) || "").trim().toLowerCase();
      return value === "1" || value === "true" || value === "yes" || value === "on";
    }

    const oneclickUiMode = uiMode === "oneclick";
    const hideBotUi = oneclickUiMode || isTruthyParam("hide_bot_ui") || isTruthyParam("hide_bot_session");
    const hideSessionUi = oneclickUiMode || isTruthyParam("hide_session_ui") || isTruthyParam("hide_bot_session");

    const statusEl = document.getElementById("status");
    const chipRowEl = document.getElementById("chipRow");
    const topBotBarEl = document.getElementById("topBotBar");
    const chatLayoutEl = document.getElementById("chatLayout");
    const sessionSidebarEl = document.getElementById("sessionSidebar");
    const botSelectEl = document.getElementById("botSelect");
    const sessionListEl = document.getElementById("sessionList");
    const chatEl = document.getElementById("chat");
    const modelSelect = document.getElementById("modelSelect");
    const activeBotChip = document.getElementById("activeBotChip");
    const activeSessionChip = document.getElementById("activeSessionChip");

    const newBotNameInput = document.getElementById("newBotName");
    const newBotIdInput = document.getElementById("newBotId");
    const createBotDropdown = document.getElementById("createBotDropdown");
    const createBotBtn = document.getElementById("createBotBtn");
    const newSessionBtn = document.getElementById("newSessionBtn");
    const refreshSessionsBtn = document.getElementById("refreshSessionsBtn");

    const chatForm = document.getElementById("chatForm");
    const msgInput = document.getElementById("msgInput");
    const chatSubmitBtn = chatForm.querySelector("button[type='submit']");
    const contextMeterBarEl = document.getElementById("contextMeterBar");
    const contextMeterLabelEl = document.getElementById("contextMeterLabel");

    const botNameInput = document.getElementById("botNameInput");
    const botModelInput = document.getElementById("botModelInput");
    const botTelegramTokenInput = document.getElementById("botTelegramTokenInput");
    const botHeartbeatInput = document.getElementById("botHeartbeatInput");
    const saveBotConfigBtn = document.getElementById("saveBotConfigBtn");

    const soulInput = document.getElementById("soulInput");
    const userInput = document.getElementById("userInput");
    const heartbeatInput = document.getElementById("heartbeatInput");
    const saveSoulBtn = document.getElementById("saveSoulBtn");
    const saveUserBtn = document.getElementById("saveUserBtn");
    const saveHeartbeatBtn = document.getElementById("saveHeartbeatBtn");
    const openaiApiKeyInput = document.getElementById("openaiApiKeyInput");
    const anthropicApiKeyInput = document.getElementById("anthropicApiKeyInput");
    const googleApiKeyInput = document.getElementById("googleApiKeyInput");
    const saveApiKeysBtn = document.getElementById("saveApiKeysBtn");
    const chipOpenAI = document.getElementById("chipOpenAI");
    const chipAnthropic = document.getElementById("chipAnthropic");
    const chipGoogle = document.getElementById("chipGoogle");
    const shellEnabledInput = document.getElementById("shellEnabledInput");
    const webEnabledInput = document.getElementById("webEnabledInput");
    const mcpEnabledInput = document.getElementById("mcpEnabledInput");
    const toolListEl = document.getElementById("toolList");
    const saveToolsBtn = document.getElementById("saveToolsBtn");
    const refreshToolsBtn = document.getElementById("refreshToolsBtn");
    const refreshEventsBtn = document.getElementById("refreshEventsBtn");
    const eventsScopedToBot = document.getElementById("eventsScopedToBot");
    const eventsAutoRefresh = document.getElementById("eventsAutoRefresh");
    const eventsLogEl = document.getElementById("eventsLog");
    const tabButtons = Array.from(document.querySelectorAll("[data-tab-target]"));
    const tabPanels = Array.from(document.querySelectorAll("[data-tab-panel]"));

    function setStatus(text) { statusEl.textContent = text; }

    function applyUiMode() {
      if (hideBotUi && topBotBarEl) {
        topBotBarEl.style.display = "none";
      }
      if (hideSessionUi) {
        if (sessionSidebarEl) sessionSidebarEl.style.display = "none";
        if (chatLayoutEl) chatLayoutEl.classList.add("chat-layout-single");
      }
      if (hideBotUi && hideSessionUi && chipRowEl) {
        chipRowEl.style.display = "none";
      }
    }

    applyUiMode();

    function addMessage(text, cls) {
      const el = document.createElement("div");
      el.className = "msg " + cls;
      el.textContent = text;
      chatEl.appendChild(el);
      chatEl.scrollTop = chatEl.scrollHeight;
    }

    function addToolTrace(toolTrace) {
      const entries = Array.isArray(toolTrace) ? toolTrace : [];
      if (!entries.length) return;

      function entryState(entry) {
        const status = String((entry || {}).status || "").trim().toLowerCase();
        if (status === "running" || status === "pending" || status === "start") return "running";
        if (status === "ok" || (entry || {}).ok === true) return "ok";
        if (status === "error" || (entry || {}).ok === false || String((entry || {}).error || "").trim()) return "error";
        return "running";
      }

      function entryKey(entry, idx) {
        const callId = String((entry || {}).call_id || "").trim();
        if (callId) return callId;
        return "tool_" + String(idx + 1) + "_" + toolEntryFingerprint(entry);
      }

      function renderToolNode(node, entry, fallbackIdx) {
        const callId = String((entry || {}).call_id || "").trim() || ("tc_" + String(fallbackIdx + 1));
        const latencyMs = Number((entry || {}).latency_ms || 0);
        const state = entryState(entry);

        node.state.className = "tool-call-state " + state;
        node.state.textContent = state === "running" ? "RUNNING" : (state === "ok" ? "OK" : "ERROR");
        node.name.textContent = String((entry || {}).tool || "tool");
        node.meta.textContent = state === "running"
          ? (callId + " \u2022 working...")
          : (callId + " \u2022 " + String(latencyMs) + "ms");

        node.payload.textContent = JSON.stringify(
          {
            status: state,
            source: String((entry || {}).source || ""),
            arguments: (entry || {}).arguments || {},
            result: (entry || {}).result || null,
            error: (entry || {}).error || null,
          },
          null,
          2
        );
      }

      function createToolNode(key) {
        const group = document.createElement("div");
        group.className = "tool-trace-group";

        const title = document.createElement("div");
        title.className = "tool-trace-title";
        title.textContent = "1 Tool Call";
        group.appendChild(title);

        const details = document.createElement("details");
        details.className = "tool-call";
        details.open = false;
        details.dataset.callKey = key;

        const summary = document.createElement("summary");
        const state = document.createElement("span");
        state.className = "tool-call-state running";
        state.textContent = "RUNNING";

        const name = document.createElement("span");
        name.className = "tool-call-name";
        name.textContent = "tool";

        const meta = document.createElement("span");
        meta.className = "tool-call-meta";
        meta.textContent = "working...";

        summary.appendChild(state);
        summary.appendChild(name);
        summary.appendChild(meta);
        details.appendChild(summary);

        const payload = document.createElement("pre");
        payload.className = "tool-call-payload";
        payload.textContent = "{}";
        details.appendChild(payload);
        group.appendChild(details);
        chatEl.appendChild(group);

        const node = { key, group, details, state, name, meta, payload };
        toolCallNodesByKey.set(key, node);
        return node;
      }

      for (let i = 0; i < entries.length; i += 1) {
        const entry = entries[i] || {};
        const key = entryKey(entry, i);
        let node = toolCallNodesByKey.get(key);
        if (!node) {
          node = createToolNode(key);
        }
        renderToolNode(node, entry, i);
      }

      chatEl.scrollTop = chatEl.scrollHeight;
    }

    function clearChat() {
      chatEl.innerHTML = "";
      toolCallNodesByKey.clear();
    }

    function setChips() {
      activeBotChip.textContent = selectedBotId ? ("BOT_ID: " + selectedBotId) : "No BOT_ID selected";
      activeSessionChip.textContent = selectedSessionId ? ("Session: " + selectedSessionId) : "No session";
    }

    function setActiveTab(tabName) {
      for (const btn of tabButtons) {
        btn.classList.toggle("active", btn.dataset.tabTarget === tabName);
      }
      for (const panel of tabPanels) {
        panel.classList.toggle("active", panel.dataset.tabPanel === tabName);
      }
      if (tabName === "tools") {
        loadToolsConfig().catch((err) => addMessage("Tools load error: " + err, "error"));
      }
      if (tabName === "events") {
        loadEventsLog().catch((err) => addMessage("Events load error: " + err, "error"));
      }
    }

    function clearBotFields() {
      botNameInput.value = "";
      botModelInput.innerHTML = "";
      botTelegramTokenInput.value = "";
      botHeartbeatInput.value = "";
      soulInput.value = "";
      userInput.value = "";
      heartbeatInput.value = "";
    }

    function modelProvider(modelId) {
      const value = String(modelId || "").trim().toLowerCase();
      for (const [prefix, provider] of Object.entries(PROVIDER_BY_PREFIX)) {
        if (value.startsWith(prefix)) return provider;
      }
      return "";
    }

    function filteredModelCatalog() {
      const enabledProviders = Object.keys(providerKeyState).filter((key) => !!providerKeyState[key]);
      if (!enabledProviders.length) return MODEL_CATALOG.slice();
      return MODEL_CATALOG.filter((model) => {
        const provider = modelProvider(model);
        return provider ? enabledProviders.includes(provider) : true;
      });
    }

    function applyProviderKeyState(healthData) {
      const openaiMasked = String((healthData || {}).openai_api_key || "").trim();
      const anthropicMasked = String((healthData || {}).anthropic_api_key || "").trim();
      const googleMasked = String((healthData || {}).google_api_key || "").trim();

      providerKeyState = {
        openai: !!openaiMasked,
        anthropic: !!anthropicMasked,
        google: !!googleMasked,
      };

      chipOpenAI.textContent = "OpenAI: " + (providerKeyState.openai ? "set" : "not set");
      chipAnthropic.textContent = "Anthropic: " + (providerKeyState.anthropic ? "set" : "not set");
      chipGoogle.textContent = "Google: " + (providerKeyState.google ? "set" : "not set");

      openaiApiKeyInput.placeholder = openaiMasked || "OpenAI API key (sk-...)";
      anthropicApiKeyInput.placeholder = anthropicMasked || "Anthropic API key (sk-ant-...)";
      googleApiKeyInput.placeholder = googleMasked || "Google API key (AIza...)";
    }

    async function loadProviderKeyState() {
      try {
        const resp = await fetch("/health");
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.error || "failed to load health");
        }
        applyProviderKeyState(data);
      } catch (err) {
        addMessage("Failed to load API key status: " + err, "error");
      }
    }

    function _populateModelSelect(selectEl, allModels, preferredValue) {
      if (!selectEl) return;
      selectEl.innerHTML = "";
      for (const model of allModels) {
        const o = document.createElement("option");
        o.value = model;
        o.textContent = model;
        selectEl.appendChild(o);
      }
      selectEl.value = preferredValue || (allModels[0] || "");
    }

    function ensureModelOptions(preferredChatModel, preferredBotModel) {
      const all = Array.from(
        new Set([preferredChatModel || "", preferredBotModel || "", ...filteredModelCatalog()])
      ).filter(Boolean);
      _populateModelSelect(modelSelect, all, preferredChatModel || preferredBotModel || "");
      _populateModelSelect(botModelInput, all, preferredBotModel || preferredChatModel || "");
    }

    function formatUpdated(ts) {
      if (typeof ts !== "number") return "";
      return new Date(ts * 1000).toLocaleString();
    }

    function formatTokenCount(n) {
      const value = Math.max(0, Number(n || 0));
      if (value >= 1000000) return (value / 1000000).toFixed(2) + "M";
      if (value >= 1000) return (value / 1000).toFixed(1) + "k";
      return String(Math.round(value));
    }

    function setContextMeter(currentTokens, maxTokens) {
      const current = Math.max(0, Number(currentTokens || 0));
      const max = Math.max(0, Number(maxTokens || 0));
      const ratio = max > 0 ? Math.max(0, Math.min(1, current / max)) : 0;
      contextMeterBarEl.style.width = String((ratio * 100).toFixed(1)) + "%";
      contextMeterBarEl.style.background =
        ratio >= 0.9 ? "#a33535" : (ratio >= 0.75 ? "#b16b21" : "#6a8693");
      contextMeterLabelEl.textContent =
        "context: " + formatTokenCount(current) + " / " + formatTokenCount(max);
      contextMeterLabelEl.title = "Estimated prompt context usage for the selected model.";
    }

    async function refreshContextUsage() {
      if (!selectedBotId) {
        setContextMeter(0, 0);
        contextMeterLabelEl.textContent = "context: -- / --";
        return;
      }

      const requestSeq = ++contextUsageRequestSeq;
      const resp = await fetch("/api/context/usage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          bot_id: selectedBotId,
          session_id: selectedSessionId || "default",
          model: modelSelect.value || "",
          draft_message: msgInput.value || "",
        }),
      });
      const data = await resp.json();
      if (requestSeq !== contextUsageRequestSeq) return;
      if (!resp.ok) throw new Error(data.error || "failed to load context usage");
      setContextMeter(data.current_tokens, data.max_tokens);
    }

    function scheduleContextUsageRefresh(delayMs = 120) {
      if (contextUsageRefreshTimer) {
        clearTimeout(contextUsageRefreshTimer);
        contextUsageRefreshTimer = null;
      }
      contextUsageRefreshTimer = setTimeout(() => {
        refreshContextUsage().catch((err) => {
          const msg = String(err || "").toLowerCase();
          if (msg.includes("bot_id is required")) {
            return;
          }
          contextMeterLabelEl.textContent = "context: unavailable";
        });
      }, Math.max(0, Number(delayMs || 0)));
    }

    function toolEntryFingerprint(entry) {
      const obj = entry || {};
      return JSON.stringify({
        call_id: String(obj.call_id || ""),
        status: String(obj.status || ""),
        tool: String(obj.tool || ""),
        source: String(obj.source || ""),
        arguments: obj.arguments || {},
        ok: !!obj.ok,
        result: obj.result || null,
        error: obj.error || null,
      });
    }

    function stopToolProgressStream() {
      if (!activeToolStream) return;
      if (activeToolStream.source) {
        try { activeToolStream.source.close(); } catch (_) {}
      }
      activeToolStream = null;
    }

    function startToolProgressStream(botId, sessionId) {
      stopToolProgressStream();
      const stream = {
        botId: String(botId || ""),
        sessionId: String(sessionId || ""),
        turnId: "",
        seen: new Set(),
        source: null,
      };
      if (!stream.botId || !stream.sessionId || typeof EventSource === "undefined") {
        activeToolStream = stream;
        return stream;
      }
      const params = new URLSearchParams({
        bot_id: stream.botId,
        session_id: stream.sessionId,
      });
      const source = new EventSource("/api/events/stream?" + params.toString());
      stream.source = source;
      source.addEventListener("tool_call_progress", (evt) => {
        let payload = {};
        try {
          payload = JSON.parse(evt.data || "{}");
        } catch (_) {
          payload = {};
        }
        const evtTurnId = String((payload || {}).turn_id || "").trim();
        if (evtTurnId && stream.turnId && evtTurnId !== stream.turnId) return;
        if (!stream.turnId && evtTurnId) stream.turnId = evtTurnId;
        const traces = Array.isArray((payload || {}).tool_trace) ? payload.tool_trace : [];
        for (const entry of traces) {
          const fp = toolEntryFingerprint(entry);
          if (stream.seen.has(fp)) continue;
          stream.seen.add(fp);
          addToolTrace([entry]);
        }
      });
      source.addEventListener("ping", () => {});
      source.onerror = () => {};
      activeToolStream = stream;
      return stream;
    }

    async function loadBots(preferredBotId) {
      const resp = await fetch("/api/bots");
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "failed to list bots");

      const bots = Array.isArray(data.bots) ? data.bots : [];
      botSelectEl.innerHTML = "";
      if (!bots.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No BOT_IDs yet";
        botSelectEl.appendChild(option);
        botSelectEl.disabled = true;
        selectedBotId = "";
        selectedSessionId = "";
        pendingSessionIds.clear();
        stopToolProgressStream();
        clearBotFields();
        setChips();
        clearChat();
        sessionListEl.innerHTML = "";
        setContextMeter(0, 0);
        contextMeterLabelEl.textContent = "context: -- / --";
        setStatus("No bots yet. Create one to start chatting.");
        return;
      }
      botSelectEl.disabled = false;

      if (preferredBotId && bots.some((b) => b.bot_id === preferredBotId)) {
        selectedBotId = preferredBotId;
      } else if (!selectedBotId || !bots.some((b) => b.bot_id === selectedBotId)) {
        selectedBotId = bots[0].bot_id;
      }

      for (const bot of bots) {
        const option = document.createElement("option");
        option.value = String(bot.bot_id || "");
        option.textContent = String(bot.name || bot.bot_id || "") + " (" + String(bot.bot_id || "") + ")";
        option.title = "Model: " + (bot.model || "") + (bot.telegram_enabled ? " | Telegram linked" : " | Telegram not linked");
        botSelectEl.appendChild(option);
      }

      if (selectedBotId) {
        botSelectEl.value = selectedBotId;
      }

      await loadSelectedBot();
      setStatus("Ready");
    }

    async function loadSelectedBot() {
      if (!selectedBotId) {
        setChips();
        setContextMeter(0, 0);
        contextMeterLabelEl.textContent = "context: -- / --";
        return;
      }
      const encodedBotId = encodeURIComponent(selectedBotId);
      const [botResp, contextResp] = await Promise.all([
        fetch("/api/bots/" + encodedBotId),
        fetch("/api/bots/" + encodedBotId + "/context")
      ]);
      const botData = await botResp.json();
      const contextData = await contextResp.json();
      if (!botResp.ok) throw new Error(botData.error || "failed to load bot");
      if (!contextResp.ok) throw new Error(contextData.error || "failed to load context");

      const bot = botData.bot || {};
      botNameInput.value = bot.name || "";
      botTelegramTokenInput.value = "";
      botHeartbeatInput.value = String(bot.heartbeat_interval_s || 300);

      ensureModelOptions(bot.model || "", bot.model || "");
      soulInput.value = (contextData.files || {})["SOUL.md"] || "";
      userInput.value = (contextData.files || {})["USER.md"] || "";
      heartbeatInput.value = (contextData.files || {})["HEARTBEAT.md"] || "";

      await loadSessions();
      setChips();
      scheduleContextUsageRefresh(40);
    }

    async function loadSessions(options = {}) {
      const refreshHistory = !(options && options.refreshHistory === false);
      if (!selectedBotId) {
        sessionListEl.innerHTML = "";
        setChips();
        setContextMeter(0, 0);
        contextMeterLabelEl.textContent = "context: -- / --";
        return;
      }
      const requestSeq = ++sessionsRequestSeq;
      const targetBotId = String(selectedBotId || "");
      const resp = await fetch("/api/sessions?bot_id=" + encodeURIComponent(targetBotId));
      const data = await resp.json();
      if (requestSeq !== sessionsRequestSeq) return;
      if (String(selectedBotId || "") !== targetBotId) return;
      if (!resp.ok) throw new Error(data.error || "failed to load sessions");
      const sessionsRaw = Array.isArray(data.sessions) ? data.sessions : [];
      const persisted = sessionsRaw.map((s) => ({
        session_id: String((s || {}).session_id || "").trim(),
        message_count: Number((s || {}).message_count || 0),
        updated_at: Number((s || {}).updated_at || 0),
        pending: false,
      })).filter((s) => !!s.session_id);
      const persistedIds = new Set(persisted.map((s) => s.session_id));
      for (const pendingId of Array.from(pendingSessionIds)) {
        if (persistedIds.has(pendingId)) pendingSessionIds.delete(pendingId);
      }
      if (!persisted.length && !selectedSessionId) {
        selectedSessionId = "session-" + Date.now();
        pendingSessionIds.add(selectedSessionId);
        clearChat();
        addMessage("Started new session: " + selectedSessionId, "system");
      }

      const pending = Array.from(pendingSessionIds)
        .filter((id) => !persistedIds.has(id))
        .map((id) => ({
          session_id: id,
          message_count: 0,
          updated_at: Date.now() / 1000,
          pending: true,
        }))
        .sort((a, b) => String(b.session_id).localeCompare(String(a.session_id)));

      const allSessions = [...pending, ...persisted];
      sessionListEl.innerHTML = "";
      if (!allSessions.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No sessions yet.";
        sessionListEl.appendChild(empty);
        setChips();
        scheduleContextUsageRefresh(40);
        return;
      }

      if (!selectedSessionId || !allSessions.some((s) => s.session_id === selectedSessionId)) {
        selectedSessionId = allSessions[0].session_id;
      }

      for (const s of allSessions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "item" + (s.session_id === selectedSessionId ? " active" : "");
        const pendingSuffix = s.pending ? " (new)" : "";
        btn.textContent = s.session_id + pendingSuffix + " (" + Number(s.message_count || 0) + ")";
        btn.title = formatUpdated(Number(s.updated_at || 0));
        btn.onclick = async () => {
          selectedSessionId = s.session_id;
          await loadSessionHistory();
          await loadSessions();
        };
        sessionListEl.appendChild(btn);
      }

      if (refreshHistory) {
        await loadSessionHistory();
      }
      setChips();
      scheduleContextUsageRefresh(40);
    }

    async function loadSessionHistory() {
      if (!selectedBotId || !selectedSessionId) {
        return;
      }
      const requestSeq = ++sessionHistoryRequestSeq;
      const targetBotId = String(selectedBotId || "");
      const targetSessionId = String(selectedSessionId || "");
      const resp = await fetch(
        "/api/sessions/" + encodeURIComponent(targetSessionId) + "?bot_id=" + encodeURIComponent(targetBotId)
      );
      const data = await resp.json();
      if (requestSeq !== sessionHistoryRequestSeq) return;
      if (String(selectedBotId || "") !== targetBotId) return;
      if (String(selectedSessionId || "") !== targetSessionId) return;
      if (!resp.ok) throw new Error(data.error || "failed to load session history");

      const toolTraceQueueByAssistant = new Map();
      try {
        const eventsResp = await fetch("/api/events?bot_id=" + encodeURIComponent(targetBotId));
        const eventsData = await eventsResp.json();
        if (requestSeq !== sessionHistoryRequestSeq) return;
        if (String(selectedBotId || "") !== targetBotId) return;
        if (String(selectedSessionId || "") !== targetSessionId) return;
        if (eventsResp.ok) {
          const events = Array.isArray(eventsData.events) ? eventsData.events.slice() : [];
          events.sort((a, b) => Number((a || {}).received_at || 0) - Number((b || {}).received_at || 0));
          for (const evt of events) {
            if (String((evt || {}).session_id || "") !== targetSessionId) continue;
            if (String((evt || {}).event_type || "").trim() === "tool_call_progress") continue;
            const traces = Array.isArray(evt.tool_trace) ? evt.tool_trace : [];
            if (!traces.length) continue;
            const assistantText = String((evt || {}).assistant_response || "");
            if (!assistantText) continue;
            const key = assistantText;
            if (!toolTraceQueueByAssistant.has(key)) {
              toolTraceQueueByAssistant.set(key, []);
            }
            toolTraceQueueByAssistant.get(key).push(traces);
          }
        }
      } catch (_) {}

      clearChat();
      const history = Array.isArray(data.history) ? data.history : [];
      if (!history.length) {
        addMessage("No messages yet for " + targetSessionId + ".", "system");
      } else {
        for (const item of history) {
          const role = String(item.role || "").trim();
          const content = String(item.content || "");
          if (!content) continue;
          if (role === "user") addMessage("You: " + content, "user");
          else if (role === "assistant") {
            addMessage("Assistant: " + content, "assistant");
            const traceQueue = toolTraceQueueByAssistant.get(content);
            if (traceQueue && traceQueue.length) {
              addToolTrace(traceQueue.shift());
            }
          }
          else addMessage(content, "system");
        }
      }
      setChips();
      scheduleContextUsageRefresh(40);
    }

    async function saveContextFile(name, value) {
      if (!selectedBotId) throw new Error("Select a bot first");
      const resp = await fetch("/api/bots/" + encodeURIComponent(selectedBotId) + "/context/" + encodeURIComponent(name), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: value })
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "failed to save " + name);
      addMessage("Saved " + name + " for " + selectedBotId, "system");
      scheduleContextUsageRefresh(40);
    }

    function renderTools(config) {
      const tools = Array.isArray((config || {}).mcp_tools) ? config.mcp_tools : [];
      shellEnabledInput.checked = !!(config || {}).shell_enabled;
      webEnabledInput.checked = !!(config || {}).web_enabled;
      mcpEnabledInput.checked = !!(config || {}).mcp_enabled;
      toolListEl.innerHTML = "";

      if (!tools.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No MCP tools available.";
        toolListEl.appendChild(empty);
        return;
      }

      tools.sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
      for (const tool of tools) {
        const name = String(tool.name || "").trim();
        const desc = String(tool.description || "").trim();
        if (!name) continue;

        const label = document.createElement("label");
        label.className = "tool-item";

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !!tool.enabled;
        checkbox.dataset.toolName = name;

        const textWrap = document.createElement("div");
        const title = document.createElement("div");
        title.className = "tool-name";
        title.textContent = name;
        const info = document.createElement("div");
        info.className = "tool-desc";
        info.textContent = desc || "No description.";

        textWrap.appendChild(title);
        textWrap.appendChild(info);
        label.appendChild(checkbox);
        label.appendChild(textWrap);
        toolListEl.appendChild(label);
      }
    }

    async function loadToolsConfig() {
      const resp = await fetch("/api/config/tools");
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "failed to load tool config");
      renderTools(data);
    }

    async function saveToolsConfig() {
      const toolMap = {};
      for (const el of toolListEl.querySelectorAll("input[data-tool-name]")) {
        toolMap[el.dataset.toolName] = !!el.checked;
      }
      const payload = {
        shell_enabled: !!shellEnabledInput.checked,
        web_enabled: !!webEnabledInput.checked,
        mcp_enabled: !!mcpEnabledInput.checked,
        mcp_tools: toolMap,
      };

      const resp = await fetch("/api/config/tools", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "failed to update tools");
      renderTools(data);
      addMessage("Tool settings saved.", "system");
    }

    async function loadEventsLog() {
      let url = "/api/events";
      if (eventsScopedToBot.checked && selectedBotId) {
        url += "?bot_id=" + encodeURIComponent(selectedBotId);
      }
      const resp = await fetch(url);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "failed to load events");
      eventsLogEl.textContent = JSON.stringify(data, null, 2);
    }

    function syncEventsAutoRefresh() {
      if (eventsRefreshTimer) {
        clearInterval(eventsRefreshTimer);
        eventsRefreshTimer = null;
      }
      if (eventsAutoRefresh.checked) {
        eventsRefreshTimer = setInterval(() => {
          loadEventsLog().catch((err) => addMessage("Events refresh error: " + err, "error"));
        }, 5000);
      }
    }

    botSelectEl.addEventListener("change", async () => {
      const nextBotId = String(botSelectEl.value || "").trim();
      if (!nextBotId || nextBotId === selectedBotId) {
        setChips();
        return;
      }
      selectedBotId = nextBotId;
      selectedSessionId = "";
      pendingSessionIds.clear();
      stopToolProgressStream();
      try {
        await loadBots(selectedBotId);
      } catch (err) {
        addMessage("Bot selection error: " + err, "error");
      }
    });

    createBotBtn.addEventListener("click", async () => {
      try {
        const payload = {};
        const nameValue = (newBotNameInput.value || "").trim();
        const botIdValue = (newBotIdInput.value || "").trim();
        if (nameValue) payload.name = nameValue;
        if (botIdValue) payload.bot_id = botIdValue;
        const resp = await fetch("/api/bots", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "failed to create bot");
        newBotNameInput.value = "";
        newBotIdInput.value = "";
        if (createBotDropdown) createBotDropdown.removeAttribute("open");
        selectedBotId = String((data.bot || {}).bot_id || "");
        selectedSessionId = "";
        pendingSessionIds.clear();
        stopToolProgressStream();
        addMessage("Created BOT_ID: " + selectedBotId, "system");
        await loadBots(selectedBotId);
      } catch (err) {
        addMessage("Create bot error: " + err, "error");
      }
    });

    newSessionBtn.addEventListener("click", async () => {
      if (!selectedBotId) {
        addMessage("Create/select a bot first.", "error");
        return;
      }
      selectedSessionId = "session-" + Date.now();
      pendingSessionIds.add(selectedSessionId);
      stopToolProgressStream();
      clearChat();
      addMessage("Started new session: " + selectedSessionId, "system");
      await loadSessions({ refreshHistory: false });
      setChips();
      scheduleContextUsageRefresh(40);
    });

    refreshSessionsBtn.addEventListener("click", async () => {
      try { await loadSessions(); } catch (err) { addMessage("Refresh sessions error: " + err, "error"); }
    });

    chatForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (chatForm.dataset.busy === "1") return;
      const text = (msgInput.value || "").trim();
      if (!text) return;
      if (!selectedBotId) {
        addMessage("Create/select a bot first.", "error");
        return;
      }
      if (!selectedSessionId) {
        selectedSessionId = "session-" + Date.now();
        pendingSessionIds.add(selectedSessionId);
      }
      chatForm.dataset.busy = "1";
      if (chatSubmitBtn) chatSubmitBtn.disabled = true;
      const turnBotId = selectedBotId;
      const turnSessionId = selectedSessionId;
      // Cancel any in-flight history render so a stale response can't overwrite this turn.
      sessionHistoryRequestSeq += 1;
      msgInput.value = "";
      addMessage("You: " + text, "user");
      setChips();
      const progressStream = startToolProgressStream(turnBotId, turnSessionId);

      try {
        const resp = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            bot_id: turnBotId,
            session_id: turnSessionId,
            message: text,
            model: modelSelect.value || ""
          })
        });
        const data = await resp.json();
        if (!resp.ok) {
          addMessage("Agent error: " + (data.error || JSON.stringify(data)), "error");
          return;
        }
        pendingSessionIds.delete(turnSessionId);
        const finalTrace = Array.isArray(data.tool_trace) ? data.tool_trace : [];
        const unseenFinalTrace = finalTrace.filter(
          (entry) => !(progressStream && progressStream.seen.has(toolEntryFingerprint(entry)))
        );
        addToolTrace(unseenFinalTrace);
        addMessage("Assistant: " + String(data.response || ""), "assistant");
        await loadSessions({ refreshHistory: false });
        scheduleContextUsageRefresh(40);
      } catch (err) {
        addMessage("Chat request error: " + err, "error");
        scheduleContextUsageRefresh(40);
      } finally {
        stopToolProgressStream();
        chatForm.dataset.busy = "0";
        if (chatSubmitBtn) chatSubmitBtn.disabled = false;
      }
    });

    saveBotConfigBtn.addEventListener("click", async () => {
      if (!selectedBotId) {
        addMessage("Create/select a bot first.", "error");
        return;
      }
      const payload = {
        name: (botNameInput.value || "").trim(),
        model: (botModelInput.value || "").trim() || (modelSelect.value || ""),
        heartbeat_interval_s: Number((botHeartbeatInput.value || "").trim() || "300"),
      };
      const tokenVal = (botTelegramTokenInput.value || "").trim();
      if (tokenVal) payload.telegram_bot_token = tokenVal;

      try {
        const resp = await fetch("/api/bots/" + encodeURIComponent(selectedBotId) + "/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "failed to save bot config");
        botTelegramTokenInput.value = "";
        addMessage("Saved bot config for " + selectedBotId, "system");
        await loadBots(selectedBotId);
      } catch (err) {
        addMessage("Save bot config error: " + err, "error");
      }
    });

    saveApiKeysBtn.addEventListener("click", async () => {
      const payload = {};
      const openaiVal = (openaiApiKeyInput.value || "").trim();
      const anthropicVal = (anthropicApiKeyInput.value || "").trim();
      const googleVal = (googleApiKeyInput.value || "").trim();

      if (openaiVal) payload.openai_api_key = openaiVal;
      if (anthropicVal) payload.anthropic_api_key = anthropicVal;
      if (googleVal) payload.google_api_key = googleVal;

      if (!Object.keys(payload).length) {
        addMessage("Enter at least one API key before saving.", "error");
        return;
      }

      try {
        const resp = await fetch("/api/config/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "failed to save API keys");

        openaiApiKeyInput.value = "";
        anthropicApiKeyInput.value = "";
        googleApiKeyInput.value = "";

        applyProviderKeyState(data);
        ensureModelOptions(modelSelect.value || "", botModelInput.value || "");
        addMessage("Settings updated successfully.", "system");
      } catch (err) {
        addMessage("Save API keys error: " + err, "error");
      }
    });

    saveToolsBtn.addEventListener("click", async () => {
      try {
        await saveToolsConfig();
      } catch (err) {
        addMessage("Save tools error: " + err, "error");
      }
    });

    refreshToolsBtn.addEventListener("click", async () => {
      try {
        await loadToolsConfig();
      } catch (err) {
        addMessage("Refresh tools error: " + err, "error");
      }
    });

    refreshEventsBtn.addEventListener("click", async () => {
      try {
        await loadEventsLog();
      } catch (err) {
        addMessage("Refresh events error: " + err, "error");
      }
    });

    eventsScopedToBot.addEventListener("change", async () => {
      try {
        await loadEventsLog();
      } catch (err) {
        addMessage("Events scope error: " + err, "error");
      }
    });

    eventsAutoRefresh.addEventListener("change", () => {
      syncEventsAutoRefresh();
    });

    msgInput.addEventListener("input", () => {
      scheduleContextUsageRefresh(120);
    });

    modelSelect.addEventListener("change", () => {
      scheduleContextUsageRefresh(40);
    });

    for (const btn of tabButtons) {
      btn.addEventListener("click", () => {
        setActiveTab(btn.dataset.tabTarget || "chat");
      });
    }

    saveSoulBtn.addEventListener("click", async () => {
      try { await saveContextFile("SOUL.md", soulInput.value || ""); } catch (err) { addMessage(String(err), "error"); }
    });
    saveUserBtn.addEventListener("click", async () => {
      try { await saveContextFile("USER.md", userInput.value || ""); } catch (err) { addMessage(String(err), "error"); }
    });
    saveHeartbeatBtn.addEventListener("click", async () => {
      try { await saveContextFile("HEARTBEAT.md", heartbeatInput.value || ""); } catch (err) { addMessage(String(err), "error"); }
    });

    (async () => {
      try {
        await loadProviderKeyState();
        setStatus("Loading bots...");
        await loadBots("");
        await loadToolsConfig();
        await loadEventsLog();
        setActiveTab("chat");
        scheduleContextUsageRefresh(10);
        setStatus("Ready");
      } catch (err) {
        setStatus("Error loading app state");
        addMessage("Bootstrap error: " + err, "error");
      }
    })();
  </script>
</body>
</html>
"""

    @app.route("/ops", methods=["GET"])
    def ops_dashboard():
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SimpleAgent Ops Dashboard</title>
  <style>
    :root {
      --bg: #0c1220;
      --panel: #161f36;
      --border: #2a385f;
      --text: #e9efff;
      --muted: #a9b6d9;
      --accent: #2d6cdf;
      --danger: #8c2f3d;
    }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
      background: radial-gradient(circle at top, #132342, var(--bg) 55%);
      color: var(--text);
    }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 16px 14px 26px; }
    h1 { margin: 0 0 8px; font-size: 24px; }
    .muted { color: var(--muted); margin: 0 0 12px; font-size: 13px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px;
    }
    .panel h3 { margin: 0 0 8px; font-size: 14px; }
    .row { display: flex; gap: 8px; margin-bottom: 8px; }
    input, select, textarea {
      width: 100%;
      box-sizing: border-box;
      background: #0f1730;
      color: var(--text);
      border: 1px solid #33477d;
      border-radius: 8px;
      padding: 8px 9px;
      font-size: 12px;
    }
    textarea { min-height: 68px; font-family: ui-monospace, Menlo, Consolas, monospace; }
    button {
      border: 1px solid #3b5eb2;
      background: var(--accent);
      color: #fff;
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 12px;
      cursor: pointer;
    }
    button.secondary { background: #22345d; border-color: #36508a; }
    button.danger { background: var(--danger); border-color: #a84a58; }
    pre {
      margin: 10px 0 0;
      background: #0a1022;
      border: 1px solid #26355a;
      border-radius: 10px;
      padding: 9px;
      min-height: 280px;
      max-height: 420px;
      overflow: auto;
      font-size: 12px;
      line-height: 1.35;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
    .chip {
      border: 1px solid #3e548f;
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 11px;
      color: #c9d5ff;
      background: #1a2850;
    }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>SimpleAgent Ops Dashboard</h1>
    <p class="muted">Queue, dead-letter, autoscale, engagement KPIs, and manual worker processing. Use the chat UI at <a href="/" style="color:#9fc0ff;">/</a> for normal bot interaction.</p>

    <div class="chips">
      <span class="chip" id="chipMode">service_mode: ?</span>
      <span class="chip" id="chipGateway">gateway: ?</span>
      <span class="chip" id="chipExecutor">executor: ?</span>
      <span class="chip" id="chipDelivery">delivery: ?</span>
      <span class="chip" id="chipPoller">poller: ?</span>
    </div>

    <div class="chips">
      <span class="chip" id="kpiActive">active_24h: ?</span>
      <span class="chip" id="kpiRetention">retention_7d: ?</span>
      <span class="chip" id="kpiSuccess">success_window: ?</span>
      <span class="chip" id="kpiEngagement">engagement_window: ?</span>
    </div>

    <div class="grid">
      <section class="panel">
        <h3>Common Inputs</h3>
        <div class="row">
          <input id="botId" type="text" placeholder="bot_id (optional where allowed)" />
          <select id="queueKind">
            <option value="both">both</option>
            <option value="inbound">inbound</option>
            <option value="outbound">outbound</option>
          </select>
          <input id="limit" type="text" value="100" />
        </div>
        <div class="row">
          <textarea id="eventIds" placeholder="event_ids (comma/newline separated)"></textarea>
        </div>
        <div class="row">
          <input id="olderThanS" type="text" placeholder="older_than_s (optional)" />
          <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#c8d2f0;">
            <input id="allowAll" type="checkbox" style="width:auto;">allow_all purge
          </label>
          <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#c8d2f0;">
            <input id="resetAttempts" type="checkbox" style="width:auto;">reset_attempts replay
          </label>
        </div>
      </section>

      <section class="panel">
        <h3>Signals</h3>
        <div class="row">
          <button id="refreshBtn" type="button">Refresh Health + Queue + Signals</button>
          <button id="autoscaleBtn" type="button" class="secondary">Get Autoscale Signals</button>
        </div>
        <div class="row">
          <input id="curExecutor" type="text" placeholder="current_executor_workers" />
          <input id="curDelivery" type="text" placeholder="current_delivery_workers" />
          <button id="recommendBtn" type="button" class="secondary">Recommendation</button>
        </div>
      </section>

      <section class="panel">
        <h3>Engagement KPI</h3>
        <div class="row">
          <input id="engWindowDays" type="text" value="7" />
          <input id="engThreshold" type="text" value="3" />
          <input id="engLimit" type="text" value="50" />
          <button id="engagementBtn" type="button" class="secondary">Load Engagement</button>
        </div>
        <p class="muted">Inputs: window_days, engaged_event_threshold, top-bot limit.</p>
      </section>

      <section class="panel">
        <h3>Dead-Letter</h3>
        <div class="row">
          <button id="listDeadBtn" type="button">List Dead-Letter</button>
          <button id="replayBtn" type="button" class="secondary">Replay</button>
          <button id="purgeBtn" type="button" class="danger">Purge</button>
        </div>
      </section>

      <section class="panel">
        <h3>Manual Processing (Executor Plane)</h3>
        <div class="row">
          <select id="processKind">
            <option value="both">both</option>
            <option value="inbound">inbound</option>
            <option value="outbound">outbound</option>
          </select>
          <input id="maxEvents" type="text" value="1" />
          <button id="processBtn" type="button">Process Once</button>
        </div>
      </section>
    </div>

    <pre id="output">Ready.</pre>
  </div>

  <script>
    const out = document.getElementById("output");
    const chipMode = document.getElementById("chipMode");
    const chipGateway = document.getElementById("chipGateway");
    const chipExecutor = document.getElementById("chipExecutor");
    const chipDelivery = document.getElementById("chipDelivery");
    const chipPoller = document.getElementById("chipPoller");
    const kpiActive = document.getElementById("kpiActive");
    const kpiRetention = document.getElementById("kpiRetention");
    const kpiSuccess = document.getElementById("kpiSuccess");
    const kpiEngagement = document.getElementById("kpiEngagement");

    function parseEventIds(text) {
      return String(text || "")
        .split(/[\\n,]/g)
        .map((s) => s.trim())
        .filter(Boolean);
    }

    function maybeInt(v) {
      const t = String(v || "").trim();
      if (!t) return null;
      const n = Number(t);
      if (!Number.isFinite(n)) return null;
      return Math.trunc(n);
    }

    function pct(v) {
      if (typeof v !== "number" || !Number.isFinite(v)) return "?";
      return v.toFixed(1) + "%";
    }

    async function req(path, method = "GET", body = null) {
      const opts = { method, headers: { "Content-Type": "application/json" } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(JSON.stringify(data));
      return data;
    }

    function append(title, data) {
      const block = "### " + title + "\\n" + JSON.stringify(data, null, 2) + "\\n\\n";
      out.textContent = block + out.textContent;
    }

    function commonInputs() {
      return {
        botId: (document.getElementById("botId").value || "").trim(),
        queue: (document.getElementById("queueKind").value || "both").trim(),
        limit: maybeInt(document.getElementById("limit").value) || 100,
        eventIds: parseEventIds(document.getElementById("eventIds").value),
        olderThanS: maybeInt(document.getElementById("olderThanS").value),
        allowAll: !!document.getElementById("allowAll").checked,
        resetAttempts: !!document.getElementById("resetAttempts").checked
      };
    }

    async function refreshChips() {
      try {
        const h = await req("/health");
        chipMode.textContent = "service_mode: " + String(h.service_mode || "?");
        const planes = h.planes || {};
        chipGateway.textContent = "gateway: " + String(!!planes.gateway);
        chipExecutor.textContent = "executor: " + String(!!planes.executor);
        chipDelivery.textContent = "delivery: " + String(!!planes.delivery_worker);
        chipPoller.textContent = "poller: " + String(!!planes.telegram_poller);
      } catch (err) {
        append("chip error", { error: String(err) });
      }
    }

    function engagementPath() {
      const w = maybeInt(document.getElementById("engWindowDays").value) || 7;
      const t = maybeInt(document.getElementById("engThreshold").value) || 3;
      const l = maybeInt(document.getElementById("engLimit").value) || 50;
      const q = new URLSearchParams({
        window_days: String(w),
        engaged_event_threshold: String(t),
        limit: String(l)
      });
      return "/api/engagement?" + q.toString();
    }

    async function refreshEngagement(writeOutput) {
      const data = await req(engagementPath());
      const e = data.engagement || {};
      const k = e.kpis || {};
      const retention = k.retention_7d || {};
      const success = k.success_rate_window || {};
      const engagement = k.engagement_rate_window || {};

      kpiActive.textContent = "active_24h: " + String(k.active_agents_24h ?? "?");
      kpiRetention.textContent =
        "retention_7d: " + pct(k.retention_7d_pct) +
        " (" + String(retention.retained_agents ?? "?") + "/" + String(retention.previous_active_agents ?? "?") + ")";
      kpiSuccess.textContent =
        "success_window: " + pct(k.success_rate_window_pct) +
        " (" + String(success.done_events ?? "?") + "/" + String(success.terminal_events ?? "?") + ")";
      kpiEngagement.textContent =
        "engagement_window: " + pct(k.engagement_rate_window_pct) +
        " (" + String(engagement.engaged_agents ?? "?") + "/" + String(engagement.active_agents_window ?? "?") + ")";

      if (writeOutput) {
        append("engagement", data);
      }
      return data;
    }

    document.getElementById("refreshBtn").addEventListener("click", async () => {
      try {
        const [health, stats, metrics, signals] = await Promise.all([
          req("/health"),
          req("/api/queue/stats"),
          req("/api/metrics"),
          req("/api/autoscale/signals")
        ]);
        append("health", health);
        append("queue_stats", stats);
        append("metrics", metrics);
        append("autoscale_signals", signals);
        await refreshEngagement(true);
        await refreshChips();
      } catch (err) {
        append("refresh error", { error: String(err) });
      }
    });

    document.getElementById("autoscaleBtn").addEventListener("click", async () => {
      try {
        append("autoscale_signals", await req("/api/autoscale/signals"));
      } catch (err) {
        append("autoscale_signals error", { error: String(err) });
      }
    });

    document.getElementById("recommendBtn").addEventListener("click", async () => {
      const curExecutor = maybeInt(document.getElementById("curExecutor").value);
      const curDelivery = maybeInt(document.getElementById("curDelivery").value);
      try {
        append(
          "autoscale_recommendation",
          await req("/api/autoscale/recommendation", "POST", {
            current_executor_workers: curExecutor,
            current_delivery_workers: curDelivery
          })
        );
      } catch (err) {
        append("autoscale_recommendation error", { error: String(err) });
      }
    });

    document.getElementById("engagementBtn").addEventListener("click", async () => {
      try {
        await refreshEngagement(true);
      } catch (err) {
        append("engagement error", { error: String(err) });
      }
    });

    document.getElementById("listDeadBtn").addEventListener("click", async () => {
      const c = commonInputs();
      const q = new URLSearchParams({ queue: c.queue, limit: String(c.limit) });
      if (c.botId) q.set("bot_id", c.botId);
      try {
        append("dead_letter_list", await req("/api/queue/dead-letter?" + q.toString()));
      } catch (err) {
        append("dead_letter_list error", { error: String(err) });
      }
    });

    document.getElementById("replayBtn").addEventListener("click", async () => {
      const c = commonInputs();
      const body = {
        queue: c.queue,
        bot_id: c.botId || undefined,
        event_ids: c.eventIds,
        limit: c.limit,
        reset_attempts: c.resetAttempts
      };
      try {
        append("dead_letter_replay", await req("/api/queue/dead-letter/replay", "POST", body));
      } catch (err) {
        append("dead_letter_replay error", { error: String(err) });
      }
    });

    document.getElementById("purgeBtn").addEventListener("click", async () => {
      const c = commonInputs();
      const body = {
        queue: c.queue,
        bot_id: c.botId || undefined,
        event_ids: c.eventIds,
        limit: c.limit,
        allow_all: c.allowAll
      };
      if (typeof c.olderThanS === "number") body.older_than_s = c.olderThanS;
      try {
        append("dead_letter_purge", await req("/api/queue/dead-letter/purge", "POST", body));
      } catch (err) {
        append("dead_letter_purge error", { error: String(err) });
      }
    });

    document.getElementById("processBtn").addEventListener("click", async () => {
      const kind = (document.getElementById("processKind").value || "both").trim();
      const maxEvents = maybeInt(document.getElementById("maxEvents").value) || 1;
      try {
        append("queue_process_once", await req("/api/queue/process-once", "POST", { kind, max_events: maxEvents }));
      } catch (err) {
        append("queue_process_once error", { error: String(err) });
      }
    });

    (async () => {
      await refreshChips();
      try {
        await refreshEngagement(false);
      } catch (err) {
        append("engagement bootstrap error", { error: String(err) });
      }
    })();
  </script>
</body>
</html>
"""

    @app.route("/engagement", methods=["GET"])
    def engagement_dashboard():
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SimpleAgent Engagement</title>
  <style>
    :root {
      --bg: #0f1222;
      --panel: #17203a;
      --line: #304575;
      --text: #ecf2ff;
      --muted: #aebce0;
      --bar: #4f7eff;
      --bar2: #55c4a8;
    }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
      background: radial-gradient(circle at top, #14284d, var(--bg) 55%);
      color: var(--text);
    }
    .wrap {
      max-width: 1040px;
      margin: 0 auto;
      padding: 18px 14px 28px;
    }
    h1 { margin: 0 0 8px; font-size: 24px; }
    .muted { margin: 0 0 12px; color: var(--muted); font-size: 13px; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
    .chip {
      border: 1px solid #3b5798;
      border-radius: 999px;
      padding: 3px 10px;
      font-size: 12px;
      color: #d7e1ff;
      background: #1b2b59;
    }
    .grid {
      display: grid;
      gap: 10px;
      grid-template-columns: 1fr;
    }
    .panel {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      padding: 10px;
    }
    .panel h3 { margin: 0 0 8px; font-size: 14px; }
    canvas {
      width: 100%;
      height: 280px;
      border: 1px solid #2b3f6d;
      border-radius: 10px;
      background: #0e1731;
      display: block;
    }
    @media (max-width: 920px) {
      canvas { height: 240px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Engagement Graphs</h1>
    <p class="muted">Auto-refresh every 30 seconds. Data from <code>/api/engagement</code>.</p>
    <div class="chips">
      <span class="chip" id="chipActive">active_24h: ?</span>
      <span class="chip" id="chipRetention">retention_7d: ?</span>
      <span class="chip" id="chipSuccess">success_7d: ?</span>
      <span class="chip" id="chipEngagement">engagement_7d: ?</span>
      <span class="chip" id="chipUpdated">updated: ?</span>
    </div>
    <div class="grid">
      <section class="panel">
        <h3>Top Bots by Events (7d)</h3>
        <canvas id="botChart" width="980" height="280"></canvas>
      </section>
      <section class="panel">
        <h3>KPI Rates (7d)</h3>
        <canvas id="kpiChart" width="980" height="280"></canvas>
      </section>
    </div>
  </div>

  <script>
    const chipActive = document.getElementById("chipActive");
    const chipRetention = document.getElementById("chipRetention");
    const chipSuccess = document.getElementById("chipSuccess");
    const chipEngagement = document.getElementById("chipEngagement");
    const chipUpdated = document.getElementById("chipUpdated");
    const botCanvas = document.getElementById("botChart");
    const kpiCanvas = document.getElementById("kpiChart");

    function pct(v) {
      if (typeof v !== "number" || !Number.isFinite(v)) return "?";
      return v.toFixed(1) + "%";
    }

    function drawBars(canvas, labels, values, opts = {}) {
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const width = canvas.width;
      const height = canvas.height;
      const padLeft = 56;
      const padRight = 20;
      const padTop = 20;
      const padBottom = 42;
      const plotW = Math.max(10, width - padLeft - padRight);
      const plotH = Math.max(10, height - padTop - padBottom);
      const maxV = Math.max(1, ...values.map((v) => Number(v) || 0));
      const count = Math.max(1, values.length);
      const stepW = plotW / count;
      const barW = Math.max(10, stepW * 0.62);

      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#0e1731";
      ctx.fillRect(0, 0, width, height);

      ctx.strokeStyle = "#2a3f6d";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const y = padTop + (plotH * i / 4);
        ctx.beginPath();
        ctx.moveTo(padLeft, y);
        ctx.lineTo(width - padRight, y);
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.moveTo(padLeft, padTop);
      ctx.lineTo(padLeft, height - padBottom);
      ctx.lineTo(width - padRight, height - padBottom);
      ctx.stroke();

      ctx.font = "11px ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif";
      ctx.fillStyle = "#adc0ee";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 4; i += 1) {
        const val = Math.round(maxV - (maxV * i / 4));
        const y = padTop + (plotH * i / 4);
        ctx.fillText(String(val), padLeft - 8, y);
      }

      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (let i = 0; i < count; i += 1) {
        const value = Math.max(0, Number(values[i]) || 0);
        const x = padLeft + (i * stepW) + ((stepW - barW) / 2);
        const h = (value / maxV) * plotH;
        const y = padTop + (plotH - h);
        ctx.fillStyle = opts.barColor || "#4f7eff";
        ctx.fillRect(x, y, barW, h);
        ctx.fillStyle = "#edf3ff";
        ctx.textBaseline = "bottom";
        ctx.fillText(String(value), x + (barW / 2), y - 2);
        ctx.fillStyle = "#c4d2f8";
        ctx.textBaseline = "top";
        const label = String(labels[i] || "").slice(0, 16);
        ctx.fillText(label, x + (barW / 2), height - padBottom + 6);
      }
    }

    async function loadEngagement() {
      const resp = await fetch("/api/engagement?window_days=7&engaged_event_threshold=3&limit=10");
      const data = await resp.json();
      if (!resp.ok || data.status !== "ok") throw new Error("failed to load engagement");

      const e = data.engagement || {};
      const k = e.kpis || {};
      const retention = k.retention_7d || {};
      const success = k.success_rate_window || {};
      const engagement = k.engagement_rate_window || {};
      const bots = Array.isArray(e.bots) ? e.bots : [];

      chipActive.textContent = "active_24h: " + String(k.active_agents_24h ?? "?");
      chipRetention.textContent =
        "retention_7d: " + pct(k.retention_7d_pct) +
        " (" + String(retention.retained_agents ?? "?") + "/" + String(retention.previous_active_agents ?? "?") + ")";
      chipSuccess.textContent =
        "success_7d: " + pct(k.success_rate_window_pct) +
        " (" + String(success.done_events ?? "?") + "/" + String(success.terminal_events ?? "?") + ")";
      chipEngagement.textContent =
        "engagement_7d: " + pct(k.engagement_rate_window_pct) +
        " (" + String(engagement.engaged_agents ?? "?") + "/" + String(engagement.active_agents_window ?? "?") + ")";
      chipUpdated.textContent = "updated: " + new Date().toLocaleTimeString();

      const botLabels = bots.map((b) => String(b.bot_name || b.bot_id || "bot"));
      const botValues = bots.map((b) => Number(b.events_count || 0));
      drawBars(botCanvas, botLabels.length ? botLabels : ["no_data"], botValues.length ? botValues : [0], { barColor: "#4f7eff" });

      const kpiLabels = ["Retention", "Success", "Engagement"];
      const kpiValues = [
        Math.round(Number(k.retention_7d_pct || 0)),
        Math.round(Number(k.success_rate_window_pct || 0)),
        Math.round(Number(k.engagement_rate_window_pct || 0)),
      ];
      drawBars(kpiCanvas, kpiLabels, kpiValues, { barColor: "#55c4a8" });
    }

    async function refresh() {
      try {
        await loadEngagement();
      } catch (err) {
        chipUpdated.textContent = "updated: error";
      }
    }

    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>
"""

    def _queue_policy_snapshot() -> Dict[str, Any]:
        return {
            "max_attempts": queue_max_attempts,
            "retry_base_s": queue_retry_base_s,
            "retry_cap_s": queue_retry_cap_s,
            "lock_timeout_s": queue_lock_timeout_s,
            "poll_interval_s": queue_poll_interval_s,
        }

    def _autoscale_policy_snapshot() -> Dict[str, Any]:
        return {
            "inbound_target_ready_per_worker": autoscale_inbound_target_ready_per_worker,
            "outbound_target_ready_per_worker": autoscale_outbound_target_ready_per_worker,
            "executor_min_workers": autoscale_executor_min_workers,
            "executor_max_workers": autoscale_executor_max_workers,
            "delivery_min_workers": autoscale_delivery_min_workers,
            "delivery_max_workers": autoscale_delivery_max_workers,
            "max_step_up": autoscale_max_step_up,
            "max_step_down": autoscale_max_step_down,
        }

    def _bounded_desired_workers(ready: int, processing: int, target: int, min_workers: int, max_workers: int) -> int:
        load = max(0, int(ready)) + max(0, int(processing))
        desired = int(math.ceil(float(load) / float(max(1, int(target))))) if load > 0 else 0
        return min(max_workers, max(min_workers, desired))

    def _autoscale_signals_snapshot() -> Dict[str, Any]:
        metrics = state.session_store.queue_metrics()
        inbound = metrics.get("inbound") if isinstance(metrics, dict) else {}
        outbound = metrics.get("outbound") if isinstance(metrics, dict) else {}
        inbound_counts = inbound.get("counts") if isinstance(inbound, dict) else {}
        outbound_counts = outbound.get("counts") if isinstance(outbound, dict) else {}

        inbound_ready = int((inbound or {}).get("queued_ready") or 0)
        outbound_ready = int((outbound or {}).get("queued_ready") or 0)
        inbound_processing = int((inbound_counts or {}).get("processing", 0))
        outbound_processing = int((outbound_counts or {}).get("processing", 0))
        inbound_dead = int((inbound_counts or {}).get("dead_letter", 0))
        outbound_dead = int((outbound_counts or {}).get("dead_letter", 0))

        desired_executor = _bounded_desired_workers(
            ready=inbound_ready,
            processing=inbound_processing,
            target=autoscale_inbound_target_ready_per_worker,
            min_workers=autoscale_executor_min_workers,
            max_workers=autoscale_executor_max_workers,
        )
        desired_delivery = _bounded_desired_workers(
            ready=outbound_ready,
            processing=outbound_processing,
            target=autoscale_outbound_target_ready_per_worker,
            min_workers=autoscale_delivery_min_workers,
            max_workers=autoscale_delivery_max_workers,
        )

        return {
            "policy": _autoscale_policy_snapshot(),
            "inbound": {
                "queued_ready": inbound_ready,
                "processing": inbound_processing,
                "dead_letter": inbound_dead,
                "oldest_queued_age_s": (inbound or {}).get("oldest_queued_age_s"),
                "hot_bots": (inbound or {}).get("queued_ready_by_bot_top", []),
                "suggested_workers": desired_executor,
            },
            "outbound": {
                "queued_ready": outbound_ready,
                "processing": outbound_processing,
                "dead_letter": outbound_dead,
                "oldest_queued_age_s": (outbound or {}).get("oldest_queued_age_s"),
                "hot_bots": (outbound or {}).get("queued_ready_by_bot_top", []),
                "suggested_workers": desired_delivery,
            },
        }

    def _parse_optional_nonneg_int(raw: Any, field_name: str) -> Optional[int]:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = int(text)
        except Exception as exc:
            raise RuntimeError(f"{field_name} must be a non-negative integer") from exc
        if value < 0:
            raise RuntimeError(f"{field_name} must be a non-negative integer")
        return value

    def _recommend_workers(
        *,
        current_workers: Optional[int],
        suggested_workers: int,
        min_workers: int,
        max_workers: int,
        max_step_up: int,
        max_step_down: int,
    ) -> Dict[str, Any]:
        bounded_suggested = max(min_workers, min(max_workers, int(suggested_workers)))
        if current_workers is None:
            return {
                "current_workers": None,
                "suggested_workers": bounded_suggested,
                "recommended_workers": bounded_suggested,
                "action": "observe_only",
                "delta": None,
            }

        current = max(min_workers, min(max_workers, int(current_workers)))
        recommended = current
        action = "hold"
        if bounded_suggested > current:
            recommended = min(bounded_suggested, current + max(1, int(max_step_up)))
            action = "scale_up" if recommended > current else "hold"
        elif bounded_suggested < current:
            recommended = max(bounded_suggested, current - max(1, int(max_step_down)))
            action = "scale_down" if recommended < current else "hold"

        return {
            "current_workers": current,
            "suggested_workers": bounded_suggested,
            "recommended_workers": recommended,
            "action": action,
            "delta": int(recommended) - int(current),
        }

    def _autoscale_recommendation_snapshot(
        current_executor_workers: Optional[int],
        current_delivery_workers: Optional[int],
    ) -> Dict[str, Any]:
        signals = _autoscale_signals_snapshot()
        policy = signals.get("policy") if isinstance(signals, dict) else {}
        inbound = signals.get("inbound") if isinstance(signals, dict) else {}
        outbound = signals.get("outbound") if isinstance(signals, dict) else {}
        executor_reco = _recommend_workers(
            current_workers=current_executor_workers,
            suggested_workers=int((inbound or {}).get("suggested_workers") or 0),
            min_workers=int((policy or {}).get("executor_min_workers") or 0),
            max_workers=int((policy or {}).get("executor_max_workers") or 100),
            max_step_up=int((policy or {}).get("max_step_up") or 10),
            max_step_down=int((policy or {}).get("max_step_down") or 10),
        )
        delivery_reco = _recommend_workers(
            current_workers=current_delivery_workers,
            suggested_workers=int((outbound or {}).get("suggested_workers") or 0),
            min_workers=int((policy or {}).get("delivery_min_workers") or 0),
            max_workers=int((policy or {}).get("delivery_max_workers") or 100),
            max_step_up=int((policy or {}).get("max_step_up") or 10),
            max_step_down=int((policy or {}).get("max_step_down") or 10),
        )
        return {
            "signals": signals,
            "recommendation": {
                "executor": executor_reco,
                "delivery": delivery_reco,
            },
        }

    def _readiness_snapshot() -> Tuple[bool, Dict[str, Any]]:
        checks: Dict[str, Any] = {}
        ready = True
        try:
            queue_stats_snapshot = state.session_store.queue_stats()
            checks["db"] = "ok"
            checks["queue"] = queue_stats_snapshot
        except Exception as exc:
            ready = False
            checks["db"] = f"error: {exc}"
            checks["queue"] = {}
        checks["gateway_plane"] = "enabled" if gateway_plane_enabled else "disabled"
        checks["executor_plane"] = "enabled" if executor_plane_enabled else "disabled"
        return ready, checks

    @app.route("/livez", methods=["GET"])
    @app.route("/api/livez", methods=["GET"])
    def livez():
        return jsonify({"status": "ok", "live": True, "service_mode": service_mode})

    @app.route("/readyz", methods=["GET"])
    @app.route("/api/readyz", methods=["GET"])
    def readyz():
        ready, checks = _readiness_snapshot()
        return (
            jsonify({"status": "ok" if ready else "error", "ready": ready, "service_mode": service_mode, "checks": checks}),
            200 if ready else 503,
        )

    @app.route("/health", methods=["GET"])
    def health():
        bots = state.session_store.list_bots(limit=200)
        return jsonify(
            {
                "status": "ok",
                "service": "simpleagent",
                "service_mode": service_mode,
                "planes": {
                    "gateway": gateway_plane_enabled,
                    "executor": executor_plane_enabled,
                    "delivery_worker": delivery_auto_run,
                    "telegram_poller": telegram_poller_enabled,
                },
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
                "bot_context_dir": os.path.abspath(bot_context_root),
                "bot_count": len(bots),
                "bots": [_mask_bot(bot) for bot in bots],
                "shell_enabled": shell_enabled_runtime,
                "web_enabled": web_enabled_runtime,
                "shell_cwd": shell_resolved_cwd,
                "shell_timeout_s": shell_timeout_s,
                "shell_max_calls_per_turn": shell_max_calls_per_turn,
                "mcp": {
                    **mcp_broker.health(),
                    "enabled": mcp_enabled_runtime,
                    "disabled_tools": sorted(mcp_disabled_tools),
                    "tools": _get_all_mcp_tools(),
                },
                "gateway_tool_url": gateway_tool_url,
                "queue": state.session_store.queue_stats(),
                "queue_policy": _queue_policy_snapshot(),
                "autoscale": _autoscale_signals_snapshot(),
                "outward_inbox_callback_url": _get_callback_url(),
                "sessions": state.snapshot().get("sessions"),
            }
        )

    @app.route("/v1/health", methods=["GET"])
    def v1_health():
        return jsonify(
            health_payload(
                runtime_kind=runtime_kind,
                runtime_version=runtime_version,
                runtime_contract_version=runtime_contract_version,
            )
        )

    @app.route("/v1/capabilities", methods=["GET"])
    def v1_capabilities():
        return jsonify(
            capabilities_payload(
                runtime_kind=runtime_kind,
                runtime_version=runtime_version,
                runtime_contract_version=runtime_contract_version,
                max_tool_passes=runtime_max_tool_passes,
                features={
                    "web_search": bool(web_enabled_runtime),
                    "web_fetch": bool(web_enabled_runtime),
                    "mcp": bool(mcp_enabled_runtime),
                    "telegram_webhook": True,
                    "streaming": False,
                    "shell": bool(shell_enabled_runtime),
                },
            )
            | {"tools_protocol": runtime_tools_protocol}
        )

    @app.route("/v1/turn", methods=["POST"])
    def v1_turn():
        trace_id = str(request.headers.get("x-trace-id", "")).strip() or f"trace_{uuid.uuid4().hex}"
        event_id = str(request.headers.get("x-event-id", "")).strip() or f"evt_{uuid.uuid4().hex}"
        try:
            payload = request.get_json(silent=True) or {}
            turn_request = parse_turn_request(payload)
        except ContractValidationError as exc:
            return (
                jsonify(
                    turn_error_payload(
                        runtime_kind=runtime_kind,
                        runtime_version=runtime_version,
                        runtime_contract_version=runtime_contract_version,
                        trace_id=trace_id,
                        event_id=event_id,
                        error=str(exc),
                    )
                ),
                400,
            )

        trace_id = turn_request.request_id or trace_id
        event_id = turn_request.request_id or event_id

        try:
            bot = _require_bot(turn_request.tenant.bot_id)
        except Exception as exc:
            return (
                jsonify(
                    turn_error_payload(
                        runtime_kind=runtime_kind,
                        runtime_version=runtime_version,
                        runtime_contract_version=runtime_contract_version,
                        trace_id=trace_id,
                        event_id=event_id,
                        error=str(exc),
                    )
                ),
                400,
            )

        selected_model = turn_request.model.model or str(bot.get("model", "")).strip() or default_model
        session_id = turn_request.tenant.session_id
        user_message = turn_request.user_message

        # Queue-only planes return deterministic queued state envelope.
        if service_mode != "all":
            queued = _enqueue_chat_event(
                bot_id=str(bot["bot_id"]),
                session_id=session_id,
                user_message=user_message,
                selected_model=selected_model,
                idempotency_key=turn_request.request_id,
                source="runtime-v1",
                source_path="/v1/turn",
            )
            return (
                jsonify(
                    {
                        "ok": True,
                        "status": "queued",
                        "trace_id": trace_id,
                        "event_id": str(queued.get("event_id", "")).strip() or event_id,
                        "runtime_kind": runtime_kind,
                        "runtime_version": runtime_version,
                        "runtime_contract_version": runtime_contract_version,
                    }
                ),
                202,
            )

        try:
            state.append_session(
                bot_id=str(bot["bot_id"]),
                session_id=session_id,
                role="user",
                content=user_message,
            )
            turn_result = _run_contract_turn(
                bot=bot,
                session_id=session_id,
                user_message=user_message,
                selected_model=selected_model,
                enabled_tools=turn_request.runtime_context.tool_policy.enabled_tools,
                disabled_tools_policy=turn_request.runtime_context.tool_policy.disabled_tools,
            )
            assistant_message = str(turn_result.get("assistant_message", "")).strip()
            state.append_session(
                bot_id=str(bot["bot_id"]),
                session_id=session_id,
                role="assistant",
                content=assistant_message,
            )
            return jsonify(
                turn_success_payload(
                    runtime_kind=runtime_kind,
                    runtime_version=runtime_version,
                    runtime_contract_version=runtime_contract_version,
                    trace_id=trace_id,
                    event_id=event_id,
                    assistant_message=assistant_message,
                    tool_trace=turn_result.get("tool_trace") or [],
                )
            )
        except Exception as exc:
            return (
                jsonify(
                    turn_error_payload(
                        runtime_kind=runtime_kind,
                        runtime_version=runtime_version,
                        runtime_contract_version=runtime_contract_version,
                        trace_id=trace_id,
                        event_id=event_id,
                        error=str(exc),
                    )
                ),
                502,
            )

    @app.route("/api/events", methods=["GET"])
    def events():
        bot_id = str(request.args.get("bot_id", "")).strip()
        clean_bot_id = _safe_bot_id(bot_id) if bot_id else None
        return jsonify({"status": "ok", **state.snapshot(bot_id=clean_bot_id)})

    @app.route("/api/events/stream", methods=["GET"])
    def events_stream():
        bot_id = _safe_bot_id(str(request.args.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400
        try:
            _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404
        session_id_filter = str(request.args.get("session_id", "")).strip()
        subscription = tool_event_stream_broker.subscribe(bot_id=bot_id)

        def _stream() -> Any:
            yield "retry: 1000\n\n"
            try:
                while True:
                    try:
                        event = subscription.get(timeout=15)
                    except queue.Empty:
                        yield "event: ping\ndata: {}\n\n"
                        continue
                    if session_id_filter and str((event or {}).get("session_id", "")).strip() != session_id_filter:
                        continue
                    payload = json.dumps(event if isinstance(event, dict) else {}, ensure_ascii=True)
                    yield f"event: tool_call_progress\ndata: {payload}\n\n"
            finally:
                tool_event_stream_broker.unsubscribe(bot_id=bot_id, q=subscription)

        return Response(
            stream_with_context(_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.route("/api/queue/stats", methods=["GET"])
    def queue_stats():
        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "planes": {
                    "gateway": gateway_plane_enabled,
                    "executor": executor_plane_enabled,
                    "delivery_worker": delivery_auto_run,
                    "telegram_poller": telegram_poller_enabled,
                },
                "queue": state.session_store.queue_stats(),
                "queue_policy": _queue_policy_snapshot(),
            }
        )

    @app.route("/api/metrics", methods=["GET"])
    def metrics():
        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "planes": {
                    "gateway": gateway_plane_enabled,
                    "executor": executor_plane_enabled,
                    "delivery_worker": delivery_auto_run,
                    "telegram_poller": telegram_poller_enabled,
                },
                "queue_metrics": state.session_store.queue_metrics(),
                "queue_policy": _queue_policy_snapshot(),
                "autoscale": _autoscale_signals_snapshot(),
            }
        )

    @app.route("/api/engagement", methods=["GET"])
    def engagement():
        window_days = _parse_optional_nonneg_int(request.args.get("window_days"), "window_days")
        if window_days is None:
            window_days = 7
        if window_days < 1:
            return jsonify({"status": "error", "error": "window_days must be >= 1"}), 400

        limit = _parse_optional_nonneg_int(request.args.get("limit"), "limit")
        if limit is None:
            limit = 50
        if limit < 1:
            return jsonify({"status": "error", "error": "limit must be >= 1"}), 400

        engaged_event_threshold = _parse_optional_nonneg_int(
            request.args.get("engaged_event_threshold"),
            "engaged_event_threshold",
        )
        if engaged_event_threshold is None:
            engaged_event_threshold = 3
        if engaged_event_threshold < 1:
            return jsonify({"status": "error", "error": "engaged_event_threshold must be >= 1"}), 400

        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "engagement": state.session_store.engagement_metrics(
                    window_days=window_days,
                    engaged_event_threshold=engaged_event_threshold,
                    limit=limit,
                ),
            }
        )

    @app.route("/api/autoscale/signals", methods=["GET"])
    def autoscale_signals():
        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "planes": {
                    "gateway": gateway_plane_enabled,
                    "executor": executor_plane_enabled,
                    "delivery_worker": delivery_auto_run,
                    "telegram_poller": telegram_poller_enabled,
                },
                "autoscale": _autoscale_signals_snapshot(),
            }
        )

    @app.route("/api/autoscale/recommendation", methods=["GET", "POST"])
    def autoscale_recommendation():
        data = request.get_json(silent=True) if request.method == "POST" else {}
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        current_executor_raw = data.get("current_executor_workers", request.args.get("current_executor_workers"))
        current_delivery_raw = data.get("current_delivery_workers", request.args.get("current_delivery_workers"))
        try:
            current_executor = _parse_optional_nonneg_int(current_executor_raw, "current_executor_workers")
            current_delivery = _parse_optional_nonneg_int(current_delivery_raw, "current_delivery_workers")
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        snapshot = _autoscale_recommendation_snapshot(
            current_executor_workers=current_executor,
            current_delivery_workers=current_delivery,
        )
        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "planes": {
                    "gateway": gateway_plane_enabled,
                    "executor": executor_plane_enabled,
                    "delivery_worker": delivery_auto_run,
                    "telegram_poller": telegram_poller_enabled,
                },
                **snapshot,
            }
        )

    @app.route("/api/queue/dead-letter", methods=["GET"])
    def queue_dead_letter():
        queue_kind = str(request.args.get("queue", "both")).strip().lower() or "both"
        try:
            parsed_limit = int(str(request.args.get("limit", "100")).strip() or "100")
        except Exception:
            parsed_limit = 100
        limit = max(1, min(500, parsed_limit))
        bot_id_raw = str(request.args.get("bot_id", "")).strip()
        bot_id = _safe_bot_id(bot_id_raw) if bot_id_raw else ""
        dead = state.session_store.list_dead_letter_events(queue=queue_kind, limit=limit, bot_id=bot_id or None)
        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "dead_letter": dead,
            }
        )

    @app.route("/api/queue/dead-letter/replay", methods=["POST"])
    def queue_dead_letter_replay():
        try:
            _ensure_executor_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400
        queue_kind = str(data.get("queue", "both")).strip().lower() or "both"
        bot_id_raw = str(data.get("bot_id", "")).strip()
        bot_id = _safe_bot_id(bot_id_raw) if bot_id_raw else ""
        ids_value = data.get("event_ids")
        event_ids: List[str] = []
        if isinstance(ids_value, list):
            event_ids = [str(x).strip() for x in ids_value if str(x).strip()]
        elif isinstance(data.get("event_id"), str) and str(data.get("event_id")).strip():
            event_ids = [str(data.get("event_id")).strip()]
        try:
            parsed_limit = int(data.get("limit", 100))
        except Exception:
            parsed_limit = 100
        limit = max(1, min(500, parsed_limit))
        reset_attempts = bool(data.get("reset_attempts", False))
        if not event_ids and not bot_id:
            return jsonify({"status": "error", "error": "Provide event_ids/event_id or bot_id"}), 400

        replayed = state.session_store.replay_dead_letter_events(
            queue=queue_kind,
            event_ids=event_ids,
            bot_id=bot_id or None,
            limit=limit,
            reset_attempts=reset_attempts,
        )
        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "replay": replayed,
                "queue": state.session_store.queue_stats(),
            }
        )

    @app.route("/api/queue/dead-letter/purge", methods=["POST"])
    def queue_dead_letter_purge():
        try:
            _ensure_executor_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        queue_kind = str(data.get("queue", "both")).strip().lower() or "both"
        bot_id_raw = str(data.get("bot_id", "")).strip()
        bot_id = _safe_bot_id(bot_id_raw) if bot_id_raw else ""
        ids_value = data.get("event_ids")
        event_ids: List[str] = []
        if isinstance(ids_value, list):
            event_ids = [str(x).strip() for x in ids_value if str(x).strip()]
        elif isinstance(data.get("event_id"), str) and str(data.get("event_id")).strip():
            event_ids = [str(data.get("event_id")).strip()]

        try:
            parsed_limit = int(data.get("limit", 100))
        except Exception:
            parsed_limit = 100
        limit = max(1, min(5000, parsed_limit))

        older_than_s: Optional[int] = None
        if "older_than_s" in data and data.get("older_than_s") is not None:
            try:
                older_than_s = max(0, int(data.get("older_than_s")))
            except Exception:
                return jsonify({"status": "error", "error": "older_than_s must be an integer"}), 400

        allow_all = bool(data.get("allow_all", False))
        if not event_ids and not bot_id and not allow_all:
            return jsonify({"status": "error", "error": "Provide event_ids/event_id or bot_id, or set allow_all=true"}), 400

        purged = state.session_store.purge_dead_letter_events(
            queue=queue_kind,
            event_ids=event_ids,
            bot_id=bot_id or None,
            limit=limit,
            older_than_s=older_than_s,
        )
        return jsonify(
            {
                "status": "ok",
                "service_mode": service_mode,
                "purge": purged,
                "queue": state.session_store.queue_stats(),
            }
        )

    @app.route("/api/queue/process-once", methods=["POST"])
    def queue_process_once():
        try:
            _ensure_executor_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503
        data = request.get_json(silent=True) or {}
        kind = str(data.get("kind", "both")).strip().lower() if isinstance(data, dict) else "both"
        max_events = 1
        if isinstance(data, dict) and "max_events" in data:
            try:
                max_events = max(1, min(50, int(data.get("max_events"))))
            except Exception:
                max_events = 1
        processed_inbound = 0
        processed_outbound = 0
        errors: List[str] = []
        for _ in range(max_events):
            try:
                did_any = False
                if kind in {"inbound", "both"}:
                    if _run_executor_once(worker_id="manual"):
                        processed_inbound += 1
                        did_any = True
                if kind in {"outbound", "both"}:
                    if _run_delivery_once(worker_id="manual"):
                        processed_outbound += 1
                        did_any = True
                if not did_any:
                    break
            except Exception as exc:
                errors.append(str(exc))
                break
        code = 200 if not errors else 502
        return (
            jsonify(
                {
                    "status": "ok" if not errors else "error",
                    "service_mode": service_mode,
                    "kind": kind,
                    "processed_inbound": processed_inbound,
                    "processed_outbound": processed_outbound,
                    "errors": errors,
                    "queue": state.session_store.queue_stats(),
                }
            ),
            code,
        )

    @app.route("/api/bots", methods=["GET"])
    def list_bots():
        bots = state.session_store.list_bots(limit=500)
        return jsonify({"status": "ok", "bots": [_mask_bot(bot) for bot in bots]})

    @app.route("/api/bots", methods=["POST"])
    def create_bot_endpoint():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        requested_bot_id = str(data.get("bot_id", "")).strip()
        bot_id = _safe_bot_id(requested_bot_id) if requested_bot_id else _new_bot_id()
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is invalid"}), 400

        name = str(data.get("name", "")).strip() or bot_id
        model = str(data.get("model", "")).strip() or default_model
        telegram_token = str(data.get("telegram_bot_token", "")).strip()
        heartbeat_interval_s = int(data.get("heartbeat_interval_s", 300) or 300)

        try:
            bot = state.session_store.create_bot(
                bot_id=bot_id,
                name=name,
                model=model,
                telegram_bot_token=telegram_token,
                heartbeat_interval_s=heartbeat_interval_s,
            )
            _ensure_bot_context_files(bot_id)
            return jsonify({"status": "ok", "bot": _mask_bot(bot)})
        except Exception as exc:
            err = str(exc)
            code = 409 if "already" in err.lower() else 400
            return jsonify({"status": "error", "error": err}), code

    @app.route("/api/bots/<bot_id>", methods=["GET"])
    def get_bot_endpoint(bot_id: str):
        try:
            bot = _require_bot(bot_id)
            return jsonify({"status": "ok", "bot": _mask_bot(bot)})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404

    @app.route("/api/bots/<bot_id>/config", methods=["POST"])
    def update_bot_config_endpoint(bot_id: str):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        try:
            _require_bot(bot_id)
            updated = state.session_store.update_bot(
                bot_id=bot_id,
                name=data.get("name") if "name" in data else None,
                model=data.get("model") if "model" in data else None,
                telegram_bot_token=data.get("telegram_bot_token") if "telegram_bot_token" in data else None,
                heartbeat_interval_s=data.get("heartbeat_interval_s") if "heartbeat_interval_s" in data else None,
            )
            _ensure_bot_context_files(str(updated["bot_id"]))
            return jsonify({"status": "ok", "bot": _mask_bot(updated)})
        except Exception as exc:
            err = str(exc)
            code = 409 if "already" in err.lower() else 400
            return jsonify({"status": "error", "error": err}), code

    @app.route("/api/bots/<bot_id>/context", methods=["GET"])
    def get_bot_context_endpoint(bot_id: str):
        try:
            _require_bot(bot_id)
            files = _load_bot_context(_safe_bot_id(bot_id))
            return jsonify({"status": "ok", "bot_id": _safe_bot_id(bot_id), "files": files})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404

    @app.route("/api/bots/<bot_id>/context/<file_name>", methods=["GET", "POST"])
    def bot_context_file_endpoint(bot_id: str, file_name: str):
        try:
            clean_bot_id = _safe_bot_id(bot_id)
            _require_bot(clean_bot_id)
            try:
                clean_name = _normalize_context_name(file_name)
            except Exception:
                return jsonify({"status": "error", "error": "context file must be SOUL.md, USER.md, or HEARTBEAT.md"}), 400

            if request.method == "GET":
                content = _read_bot_context_file(clean_bot_id, clean_name)
                return jsonify({"status": "ok", "bot_id": clean_bot_id, "file": clean_name, "content": content})

            data = request.get_json(silent=True) or {}
            content = str(data.get("content", ""))
            _write_bot_context_file(clean_bot_id, clean_name, content)
            return jsonify({"status": "ok", "bot_id": clean_bot_id, "file": clean_name, "content": content})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

    @app.route("/api/bots/<bot_id>/heartbeat", methods=["GET", "POST"])
    def bot_heartbeat_endpoint(bot_id: str):
        try:
            clean_bot_id = _safe_bot_id(bot_id)
            bot = _require_bot(clean_bot_id)
            if request.method == "GET":
                content = _read_bot_context_file(clean_bot_id, "HEARTBEAT.md")
                return jsonify(
                    {
                        "status": "ok",
                        "bot_id": clean_bot_id,
                        "heartbeat_interval_s": int(bot.get("heartbeat_interval_s") or 300),
                        "heartbeat": content,
                    }
                )

            data = request.get_json(silent=True) or {}
            content = str(data.get("content", "")).strip()
            if content:
                _write_bot_context_file(clean_bot_id, "HEARTBEAT.md", content)
            interval = data.get("interval_s")
            if interval is not None:
                bot = state.session_store.update_bot(clean_bot_id, heartbeat_interval_s=max(1, int(interval)))
            content_after = _read_bot_context_file(clean_bot_id, "HEARTBEAT.md")
            return jsonify(
                {
                    "status": "ok",
                    "bot_id": clean_bot_id,
                    "heartbeat_interval_s": int(bot.get("heartbeat_interval_s") or 300),
                    "heartbeat": content_after,
                }
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

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
        try:
            _ensure_executor_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503
        data = request.get_json(silent=True) or {}
        tool = str(data.get("tool", "")).strip()
        arguments = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
        try:
            bot_id = _require_bot_id_from_request(data, required=True)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400
        if not tool:
            return jsonify({"status": "error", "error": "tool is required"}), 400
        if not mcp_enabled_runtime:
            return jsonify({"status": "error", "error": "MCP is disabled"}), 400
        if tool in mcp_disabled_tools:
            return jsonify({"status": "error", "error": f"MCP tool '{tool}' is disabled"}), 400
        try:
            _require_bot(bot_id)
            result = mcp_broker.call_tool(full_name=tool, arguments=arguments, bot_id=bot_id)
            return jsonify({"status": "ok", "bot_id": bot_id, "tool": tool, "result": result})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "tool": tool, "bot_id": bot_id}), 502

    @app.route("/api/config/tools", methods=["GET"])
    def get_tools_config():
        tools = _get_all_mcp_tools()
        return jsonify(
            {
                "status": "ok",
                "shell_enabled": shell_enabled_runtime,
                "web_enabled": web_enabled_runtime,
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
        nonlocal shell_enabled_runtime, web_enabled_runtime, mcp_enabled_runtime, mcp_disabled_tools
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        if "shell_enabled" in data:
            shell_enabled_runtime = bool(data.get("shell_enabled"))
            os.environ["SIMPLEAGENT_SHELL_ENABLED"] = "1" if shell_enabled_runtime else "0"
            _upsert_env_var(".env", "SIMPLEAGENT_SHELL_ENABLED", "1" if shell_enabled_runtime else "0")

        if "web_enabled" in data:
            web_enabled_runtime = bool(data.get("web_enabled"))
            os.environ["SIMPLEAGENT_WEB_ENABLED"] = "1" if web_enabled_runtime else "0"
            _upsert_env_var(".env", "SIMPLEAGENT_WEB_ENABLED", "1" if web_enabled_runtime else "0")

        if "mcp_enabled" in data:
            mcp_enabled_runtime = bool(data.get("mcp_enabled"))
            os.environ["SIMPLEAGENT_MCP_ENABLED"] = "1" if mcp_enabled_runtime else "0"
            _upsert_env_var(".env", "SIMPLEAGENT_MCP_ENABLED", "1" if mcp_enabled_runtime else "0")

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
            os.environ["SIMPLEAGENT_MCP_DISABLED_TOOLS"] = disabled_csv
            _upsert_env_var(".env", "SIMPLEAGENT_MCP_DISABLED_TOOLS", disabled_csv)

        return get_tools_config()

    @app.route("/hooks/videomemory-alert", methods=["POST"])
    def videomemory_hook():
        try:
            _ensure_gateway_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503
        expected = gateway_token
        if expected:
            auth_header = request.headers.get("Authorization", "")
            expected_header = f"Bearer {expected}"
            if auth_header != expected_header:
                return jsonify({"status": "error", "error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        bot_id = _safe_bot_id(str(payload.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400
        try:
            bot = _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404

        note = str(payload.get("note", "")).strip()
        io_id = str(payload.get("io_id", "")).strip() or "unknown"
        task_id = str(payload.get("task_id", "")).strip() or "unknown"
        task_desc = str(payload.get("task_description", "")).strip()
        session_id = _build_hook_session_id(
            hook_name="videomemory",
            primary=io_id,
            secondary=task_id,
            payload=payload,
        )

        rendered = f"VideoMemory alert on {io_id} task {task_id}: {note or '(empty note)'}"
        if task_desc:
            rendered = f"{rendered}\nTask: {task_desc}"
        selected_model = (
            str(payload.get("model", "")).strip()
            or str(bot.get("model", "")).strip()
            or default_model
        )
        idempotency_key = (
            str(payload.get("idempotency_key", "")).strip()
            or str(payload.get("event_id", "")).strip()
            or str(request.headers.get("Idempotency-Key", "")).strip()
        )
        wait_for_response = bool(payload.get("wait_for_response", service_mode == "all"))
        try:
            event = _enqueue_chat_event(
                bot_id=bot_id,
                session_id=session_id,
                user_message=rendered,
                selected_model=selected_model,
                idempotency_key=idempotency_key or None,
                source="videomemory-hook",
                source_path="/hooks/videomemory-alert",
            )
            if service_mode == "all":
                result = _execute_inbound_event(event)
                return jsonify(
                    {
                        **result,
                        "event_id": event["event_id"],
                        "hook": "videomemory-alert",
                    }
                )

            if wait_for_response:
                result = _wait_for_inbound_result(event["event_id"], timeout_s=chat_sync_timeout_s)
                if result is not None:
                    return jsonify(
                        {
                            **result,
                            "event_id": event["event_id"],
                            "hook": "videomemory-alert",
                        }
                    )

            return (
                jsonify(
                    {
                        "status": "queued",
                        "bot_id": bot_id,
                        "session_id": session_id,
                        "event_id": event["event_id"],
                        "hook": "videomemory-alert",
                    }
                ),
                202,
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "bot_id": bot_id, "session_id": session_id}), 502

    @app.route("/hooks/outward_inbox", methods=["POST"])
    def outward_inbox_hook():
        try:
            _ensure_gateway_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raw_text = (request.get_data(as_text=True) or "").strip()
            payload = {"raw": _truncate_text(raw_text, 8000)} if raw_text else {}

        bot_id = _safe_bot_id(str(payload.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400
        try:
            bot = _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404

        source = str(payload.get("source", "")).strip() or "outward"
        source_key = re.sub(r"[^a-zA-Z0-9:_-]+", "-", source).strip("-") or "outward"
        session_id = _build_hook_session_id(
            hook_name="outward",
            primary=source_key,
            payload=payload,
        )
        note = (
            str(payload.get("message", "")).strip()
            or str(payload.get("note", "")).strip()
            or str(payload.get("text", "")).strip()
            or json.dumps(payload, ensure_ascii=True)
        )
        rendered = f"Outward inbox notification from {source}: {note}"
        selected_model = (
            str(payload.get("model", "")).strip()
            or str(bot.get("model", "")).strip()
            or default_model
        )
        idempotency_key = (
            str(payload.get("idempotency_key", "")).strip()
            or str(payload.get("event_id", "")).strip()
            or str(request.headers.get("Idempotency-Key", "")).strip()
        )
        wait_for_response = bool(payload.get("wait_for_response", service_mode == "all"))
        try:
            event = _enqueue_chat_event(
                bot_id=bot_id,
                session_id=session_id,
                user_message=rendered,
                selected_model=selected_model,
                idempotency_key=idempotency_key or None,
                source=f"outward-hook:{source_key}",
                source_path="/hooks/outward_inbox",
            )
            if service_mode == "all":
                result = _execute_inbound_event(event)
                return jsonify(
                    {
                        **result,
                        "event_id": event["event_id"],
                        "hook": "outward_inbox",
                    }
                )

            if wait_for_response:
                result = _wait_for_inbound_result(event["event_id"], timeout_s=chat_sync_timeout_s)
                if result is not None:
                    return jsonify(
                        {
                            **result,
                            "event_id": event["event_id"],
                            "hook": "outward_inbox",
                        }
                    )

            return (
                jsonify(
                    {
                        "status": "queued",
                        "bot_id": bot_id,
                        "session_id": session_id,
                        "event_id": event["event_id"],
                        "hook": "outward_inbox",
                    }
                ),
                202,
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "bot_id": bot_id, "session_id": session_id}), 502

    @app.route("/api/sessions/<session_id>", methods=["GET"])
    def get_session(session_id: str):
        session_id = session_id.strip()
        if not session_id:
            return jsonify({"status": "error", "error": "session_id is required"}), 400
        bot_id = _safe_bot_id(str(request.args.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400
        try:
            _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404
        return jsonify({"status": "ok", "bot_id": bot_id, "session_id": session_id, "history": state.get_session(bot_id, session_id)})

    @app.route("/api/sessions/<session_id>", methods=["DELETE"])
    def delete_session(session_id: str):
        session_id = session_id.strip()
        if not session_id:
            return jsonify({"status": "error", "error": "session_id is required"}), 400
        bot_id = _safe_bot_id(str(request.args.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400
        try:
            _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404
        deleted = state.delete_session(bot_id=bot_id, session_id=session_id)
        return jsonify({"status": "ok", "bot_id": bot_id, "session_id": session_id, "deleted_messages": deleted})

    @app.route("/api/sessions", methods=["GET"])
    def list_sessions():
        bot_id = _safe_bot_id(str(request.args.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400
        try:
            _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 404
        return jsonify({"status": "ok", "bot_id": bot_id, "sessions": state.list_sessions(bot_id)})

    @app.route("/api/context/usage", methods=["POST"])
    def chat_context():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        try:
            bot_id = _require_bot_id_from_request(data, required=True)
            bot = _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        session_id = str(data.get("session_id", "")).strip() or "default"
        selected_model = str(data.get("model", "")).strip() or str(bot.get("model", "")).strip() or default_model
        draft_message = str(data.get("draft_message", ""))

        try:
            messages = _build_llm_messages(bot=bot, session_id=session_id, user_message=draft_message)
            current_tokens = _estimate_tokens_for_messages(messages)
            max_tokens = _max_context_tokens_for_model(selected_model)
            remaining_tokens = max(0, int(max_tokens) - int(current_tokens))
            usage_ratio = 0.0
            if max_tokens > 0:
                usage_ratio = max(0.0, min(1.0, float(current_tokens) / float(max_tokens)))

            return jsonify(
                {
                    "status": "ok",
                    "estimated": True,
                    "bot_id": bot_id,
                    "session_id": session_id,
                    "model": selected_model,
                    "current_tokens": int(current_tokens),
                    "max_tokens": int(max_tokens),
                    "remaining_tokens": int(remaining_tokens),
                    "usage_ratio": usage_ratio,
                }
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "bot_id": bot_id, "session_id": session_id}), 502

    @app.route("/api/config/telegram", methods=["POST"])
    def update_telegram_config():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400
        bot_id = _safe_bot_id(str(data.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400
        token = str(data.get("telegram_bot_token", "")).strip()
        try:
            updated = state.session_store.update_bot(bot_id=bot_id, telegram_bot_token=token)
            return jsonify(
                {
                    "status": "ok",
                    "bot_id": bot_id,
                    "telegram_enabled": _telegram_token_ready(str(updated.get("telegram_bot_token", ""))),
                    "telegram_bot_token": _mask_secret(str(updated.get("telegram_bot_token", ""))),
                    "message": "telegram_bot_token updated",
                }
            )
        except Exception as exc:
            err = str(exc)
            code = 409 if "already" in err.lower() else 400
            return jsonify({"status": "error", "error": err}), code

    @app.route("/api/config/settings", methods=["POST"])
    def update_settings():
        nonlocal openai_api_key, anthropic_api_key, google_api_key
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

        if "bot_id" in data and "telegram_bot_token" in data:
            bot_id = _safe_bot_id(str(data.get("bot_id", "")).strip())
            if bot_id:
                try:
                    state.session_store.update_bot(bot_id=bot_id, telegram_bot_token=str(data.get("telegram_bot_token", "")).strip())
                except Exception as exc:
                    return jsonify({"status": "error", "error": str(exc)}), 400

        os.environ["OPENAI_API_KEY"] = openai_api_key
        os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key
        os.environ["GOOGLE_API_KEY"] = google_api_key

        _upsert_env_var(".env", "OPENAI_API_KEY", openai_api_key)
        _upsert_env_var(".env", "ANTHROPIC_API_KEY", anthropic_api_key)
        _upsert_env_var(".env", "GOOGLE_API_KEY", google_api_key)

        return jsonify(
            {
                "status": "ok",
                "openai_api_key": _mask_secret(openai_api_key),
                "anthropic_api_key": _mask_secret(anthropic_api_key),
                "google_api_key": _mask_secret(google_api_key),
                "message": "Settings updated",
            }
        )

    @app.route("/api/telegram/webhook/<bot_id>", methods=["POST"])
    def telegram_webhook(bot_id: str):
        try:
            _ensure_gateway_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400
        message = payload.get("message")
        if not isinstance(message, dict):
            return jsonify({"status": "ignored", "reason": "missing message"}), 200
        update_id_raw = payload.get("update_id")
        update_id: Optional[int] = None
        if isinstance(update_id_raw, (int, str)) and str(update_id_raw).strip():
            try:
                update_id = int(update_id_raw)
            except ValueError:
                update_id = None
        try:
            clean_bot_id = _safe_bot_id(bot_id)
            event = _enqueue_telegram_event(
                bot_id=clean_bot_id,
                message=message,
                update_id=update_id,
                source_path=f"/api/telegram/webhook/{clean_bot_id}",
            )
            if service_mode == "all":
                result = _execute_inbound_event(event)
                return jsonify(result)
            return (
                jsonify(
                    {
                        "status": "queued",
                        "bot_id": clean_bot_id,
                        "event_id": event["event_id"],
                    }
                ),
                202,
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "bot_id": _safe_bot_id(bot_id)}), 502

    @app.route("/api/telegram/inbound", methods=["POST"])
    def telegram_inbound():
        try:
            _ensure_gateway_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503
        expected = gateway_token
        if expected:
            auth_header = request.headers.get("Authorization", "")
            if auth_header != f"Bearer {expected}":
                return jsonify({"status": "error", "error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        bot_id = _safe_bot_id(str(payload.get("bot_id", "")).strip())
        if not bot_id:
            return jsonify({"status": "error", "error": "bot_id is required"}), 400

        update = payload.get("update")
        message = payload.get("message")
        update_id: Optional[int] = None
        if isinstance(update, dict):
            message = update.get("message") if isinstance(update.get("message"), dict) else message
            update_id_raw = update.get("update_id")
            if isinstance(update_id_raw, (int, str)) and str(update_id_raw).strip():
                try:
                    update_id = int(update_id_raw)
                except ValueError:
                    update_id = None

        if isinstance(payload.get("update_id"), (int, str)) and str(payload.get("update_id")).strip():
            try:
                update_id = int(payload.get("update_id"))
            except ValueError:
                pass

        if not isinstance(message, dict):
            return jsonify({"status": "error", "error": "message object is required"}), 400

        try:
            event = _enqueue_telegram_event(
                bot_id=bot_id,
                message=message,
                update_id=update_id,
                source_path="/api/telegram/inbound",
            )
            if service_mode == "all":
                result = _execute_inbound_event(event)
                return jsonify(result)
            return (
                jsonify(
                    {
                        "status": "queued",
                        "bot_id": bot_id,
                        "event_id": event["event_id"],
                    }
                ),
                202,
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "bot_id": bot_id}), 502

    @app.route("/api/chat", methods=["POST"])
    def chat():
        try:
            _ensure_gateway_plane()
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 503
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "error": "JSON body is required"}), 400

        try:
            bot_id = _require_bot_id_from_request(data, required=True)
            bot = _require_bot(bot_id)
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 400

        session_id = str(data.get("session_id", "")).strip() or "default"
        user_message = str(data.get("message", "")).strip()
        selected_model = str(data.get("model", "")).strip() or str(bot.get("model", "")).strip() or default_model
        if not user_message:
            return jsonify({"status": "error", "error": "message is required", "bot_id": bot_id}), 400

        idempotency_key = str(data.get("idempotency_key", "")).strip() or str(request.headers.get("Idempotency-Key", "")).strip()
        wait_for_response = bool(data.get("wait_for_response", service_mode == "all"))

        try:
            event = _enqueue_chat_event(
                bot_id=bot_id,
                session_id=session_id,
                user_message=user_message,
                selected_model=selected_model,
                idempotency_key=idempotency_key or None,
            )

            if service_mode == "all":
                result = _execute_inbound_event(event)
                return jsonify(result)

            if wait_for_response:
                result = _wait_for_inbound_result(event["event_id"], timeout_s=chat_sync_timeout_s)
                if result is not None:
                    return jsonify(result)

            return (
                jsonify(
                    {
                        "status": "queued",
                        "bot_id": bot_id,
                        "session_id": session_id,
                        "event_id": event["event_id"],
                    }
                ),
                202,
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc), "bot_id": bot_id, "session_id": session_id}), 502

    if executor_plane_enabled and executor_auto_run:
        executor_thread = threading.Thread(
            target=_run_executor_loop,
            args=("executor-auto",),
            name="simpleagent-executor",
            daemon=True,
        )
        executor_thread.start()

    if executor_plane_enabled and delivery_auto_run:
        delivery_thread = threading.Thread(
            target=_run_delivery_loop,
            args=("delivery-auto",),
            name="simpleagent-delivery",
            daemon=True,
        )
        delivery_thread.start()

    if gateway_plane_enabled and telegram_poller_enabled:
        poller_thread = threading.Thread(
            target=_run_telegram_poller_loop,
            args=("telegram-poller-auto",),
            name="simpleagent-telegram-poller",
            daemon=True,
        )
        poller_thread.start()

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "18789"))
    app.run(host=host, port=port, debug=False, threaded=True)
