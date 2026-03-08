#!/usr/bin/env python3
"""Gateway process entrypoint for SimpleAgent split deployment."""

import os

os.environ.setdefault("SIMPLEAGENT_SERVICE_MODE", "gateway")
os.environ.setdefault("SIMPLEAGENT_EXECUTOR_AUTO_RUN", "0")
os.environ.setdefault("SIMPLEAGENT_DELIVERY_AUTO_RUN", "0")
os.environ.setdefault("SIMPLEAGENT_TELEGRAM_POLLER_ENABLED", "0")

from app import create_app  # noqa: E402


if __name__ == "__main__":
    app = create_app()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "18789"))
    app.run(host=host, port=port, debug=False, threaded=True)
