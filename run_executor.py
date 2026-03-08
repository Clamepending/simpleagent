#!/usr/bin/env python3
"""Executor + delivery worker process entrypoint for split deployment."""

import os
import time

os.environ.setdefault("SIMPLEAGENT_SERVICE_MODE", "executor")
os.environ.setdefault("SIMPLEAGENT_EXECUTOR_AUTO_RUN", "1")
os.environ.setdefault("SIMPLEAGENT_DELIVERY_AUTO_RUN", "1")
os.environ.setdefault("SIMPLEAGENT_TELEGRAM_POLLER_ENABLED", "0")

from app import create_app  # noqa: E402


if __name__ == "__main__":
    # Worker loops are started by create_app() based on env flags.
    create_app()
    while True:
        time.sleep(3600)
