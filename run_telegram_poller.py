#!/usr/bin/env python3
"""Dedicated Telegram poller process entrypoint for split deployment."""

import os
import time

os.environ.setdefault("SIMPLEAGENT_SERVICE_MODE", "gateway")
os.environ.setdefault("SIMPLEAGENT_EXECUTOR_AUTO_RUN", "0")
os.environ.setdefault("SIMPLEAGENT_DELIVERY_AUTO_RUN", "0")
os.environ.setdefault("SIMPLEAGENT_TELEGRAM_POLLER_ENABLED", "1")

from app import create_app  # noqa: E402


if __name__ == "__main__":
    # Poller loop is started by create_app() based on env flags.
    create_app()
    while True:
        time.sleep(3600)
