#!/usr/bin/env python3
"""Generate queued chat load and print autoscale signals."""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

import requests


def _post_json(base_url: str, path: str, payload: Dict[str, Any], timeout_s: int = 15) -> Dict[str, Any]:
    resp = requests.post(f"{base_url}{path}", json=payload, timeout=timeout_s)
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        raise RuntimeError(f"{path} failed ({resp.status_code}): {json.dumps(data, ensure_ascii=True)}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} returned non-JSON object")
    return data


def _get_json(base_url: str, path: str, timeout_s: int = 15) -> Dict[str, Any]:
    resp = requests.get(f"{base_url}{path}", timeout=timeout_s)
    data = resp.json() if resp.text else {}
    if resp.status_code >= 400:
        raise RuntimeError(f"{path} failed ({resp.status_code}): {json.dumps(data, ensure_ascii=True)}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} returned non-JSON object")
    return data


def _ensure_bot(base_url: str, bot_id: str, bot_name: str, model: str) -> str:
    if bot_id:
        return bot_id
    created = _post_json(base_url, "/api/bots", {"name": bot_name, "model": model})
    bot = created.get("bot") if isinstance(created.get("bot"), dict) else {}
    result = str(bot.get("bot_id", "")).strip()
    if not result:
        raise RuntimeError("failed to create bot_id")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Burst /api/chat queue traffic and print autoscale signals.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18789", help="Service URL (default: %(default)s)")
    parser.add_argument("--bot-id", default="", help="Existing BOT_ID. If omitted, a temporary bot is created.")
    parser.add_argument("--bot-name", default="load-bot", help="Bot name when creating a bot.")
    parser.add_argument("--model", default="gpt-4o-mini", help="Model when creating a bot.")
    parser.add_argument("--requests", type=int, default=200, help="Total /api/chat requests to enqueue.")
    parser.add_argument("--concurrency", type=int, default=20, help="Concurrent request workers.")
    parser.add_argument("--sessions", type=int, default=20, help="Session shards across the load.")
    parser.add_argument("--message", default="load-test message", help="Message content.")
    parser.add_argument("--poll-seconds", type=int, default=20, help="How long to poll signals after enqueue.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Signal poll interval in seconds.")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    total = max(1, int(args.requests))
    concurrency = max(1, int(args.concurrency))
    sessions = max(1, int(args.sessions))
    poll_seconds = max(1, int(args.poll_seconds))
    poll_interval = max(0.2, float(args.poll_interval))

    bot_id = _ensure_bot(base_url, args.bot_id.strip(), args.bot_name.strip() or "load-bot", args.model.strip() or "gpt-4o-mini")
    print(json.dumps({"event": "bot_ready", "bot_id": bot_id}, ensure_ascii=True))

    sent = 0
    failed = 0
    lock = threading.Lock()
    start = time.time()

    def _send_one(i: int) -> None:
        nonlocal sent, failed
        session_id = f"load:{i % sessions}"
        idem = f"load:{bot_id}:{i}:{uuid.uuid4().hex}"
        payload = {
            "bot_id": bot_id,
            "session_id": session_id,
            "message": args.message,
            "wait_for_response": False,
            "idempotency_key": idem,
        }
        try:
            data = _post_json(base_url, "/api/chat", payload, timeout_s=20)
            status = str(data.get("status", "")).strip().lower()
            if status not in {"queued", "ok"}:
                raise RuntimeError(f"unexpected status: {status}")
            with lock:
                sent += 1
        except Exception:
            with lock:
                failed += 1

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_send_one, i) for i in range(total)]
        for _ in as_completed(futures):
            pass

    enqueue_elapsed = time.time() - start
    print(
        json.dumps(
            {
                "event": "enqueue_complete",
                "bot_id": bot_id,
                "sent": sent,
                "failed": failed,
                "elapsed_s": round(enqueue_elapsed, 3),
                "rps": round(float(sent) / max(0.001, enqueue_elapsed), 2),
            },
            ensure_ascii=True,
        )
    )

    deadline = time.time() + poll_seconds
    while time.time() < deadline:
        try:
            autoscale = _get_json(base_url, "/api/autoscale/signals")
            stats = _get_json(base_url, "/api/queue/stats")
            print(
                json.dumps(
                    {
                        "event": "signal",
                        "ts": time.time(),
                        "autoscale": autoscale.get("autoscale"),
                        "queue": stats.get("queue"),
                    },
                    ensure_ascii=True,
                )
            )
        except Exception as exc:
            print(json.dumps({"event": "signal_error", "error": str(exc)}, ensure_ascii=True))
        time.sleep(poll_interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
