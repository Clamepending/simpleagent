# SimpleAgent

SimpleAgent is a local-first AI chat service.

It receives chat messages, keeps per-session history in a local SQLite database, generates responses from an
OpenAI-compatible endpoint, and can optionally forward assistant replies downstream.

## Run Locally (recommended for fast iteration)

`app.py` auto-loads `.env`, so local testing works without manual export.

1) Create/update `.env`:

```bash
PORT=18789
SIMPLEAGENT_LLM_URL=http://127.0.0.1:11434/v1/chat/completions
SIMPLEAGENT_MODEL=llama3.2
SIMPLEAGENT_SERVICE_MODE=all
SIMPLEAGENT_EXECUTOR_AUTO_RUN=0
SIMPLEAGENT_DELIVERY_AUTO_RUN=0
SIMPLEAGENT_TELEGRAM_POLLER_ENABLED=0
SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S=35
SIMPLEAGENT_QUEUE_POLL_INTERVAL_MS=200
SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S=120
SIMPLEAGENT_QUEUE_MAX_ATTEMPTS=5
SIMPLEAGENT_QUEUE_RETRY_BASE_S=2
SIMPLEAGENT_QUEUE_RETRY_CAP_S=60
SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER=20
SIMPLEAGENT_AUTOSCALE_OUTBOUND_TARGET_READY_PER_WORKER=40
SIMPLEAGENT_AUTOSCALE_EXECUTOR_MIN_WORKERS=0
SIMPLEAGENT_AUTOSCALE_EXECUTOR_MAX_WORKERS=100
SIMPLEAGENT_AUTOSCALE_DELIVERY_MIN_WORKERS=0
SIMPLEAGENT_AUTOSCALE_DELIVERY_MAX_WORKERS=100
SIMPLEAGENT_AUTOSCALE_MAX_STEP_UP=10
SIMPLEAGENT_AUTOSCALE_MAX_STEP_DOWN=10
OPENAI_API_KEY=replace-with-your-openai-api-key
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
SIMPLEAGENT_LLM_API_KEY=
SIMPLEAGENT_DB_PATH=./simpleagent.db
SIMPLEAGENT_SHELL_ENABLED=0
SIMPLEAGENT_WEB_ENABLED=1
SIMPLEAGENT_SHELL_CWD=.
SIMPLEAGENT_SHELL_TIMEOUT_S=20
SIMPLEAGENT_SHELL_MAX_OUTPUT_CHARS=8000
SIMPLEAGENT_SHELL_MAX_CALLS_PER_TURN=3
SIMPLEAGENT_FORWARD_ENABLED=0
SIMPLEAGENT_FORWARD_URL=http://127.0.0.1:8000/hooks/inbox
SIMPLEAGENT_FORWARD_TOKEN=replace-with-your-downstream-api-key
SIMPLEAGENT_TELEGRAM_POLL_TIMEOUT_S=25
SIMPLEAGENT_TELEGRAM_POLL_RETRY_S=5
```

2) Start the app with `uv` (uses pinned dependencies from `uv.lock`):

```bash
uv sync --frozen
uv run python app.py
```

App URL: `http://localhost:18789`

## Run With Docker

If your downstream service is running on your laptop, use:

```bash
docker compose --env-file .env.docker.example up --build
```

In Docker mode, `SIMPLEAGENT_FORWARD_URL` should usually use `host.docker.internal`.
For a local model on your laptop, `SIMPLEAGENT_LLM_URL` should also use `host.docker.internal`.

## Environment

- `PORT` (default: `18789`)
- `SIMPLEAGENT_LLM_URL` (OpenAI-compatible chat completions endpoint)
- `SIMPLEAGENT_MODEL` (model name sent to LLM endpoint)
- `SIMPLEAGENT_SERVICE_MODE` (`all`, `gateway`, or `executor`; default `all`)
- `SIMPLEAGENT_EXECUTOR_AUTO_RUN` (default: `0` unless `SIMPLEAGENT_SERVICE_MODE=executor`; auto-start inbound executor worker loop)
- `SIMPLEAGENT_DELIVERY_AUTO_RUN` (default: `0` unless `SIMPLEAGENT_SERVICE_MODE=executor`; auto-start outbound delivery worker loop)
- `SIMPLEAGENT_TELEGRAM_POLLER_ENABLED` (default: `0`; enable gateway-plane Telegram long poller loop)
- `SIMPLEAGENT_CHAT_SYNC_TIMEOUT_S` (default: `35`; gateway wait timeout when `wait_for_response=true`)
- `SIMPLEAGENT_QUEUE_POLL_INTERVAL_MS` (default: `200`; queue worker idle poll interval)
- `SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S` (default: `120`; stale processing lock timeout before automatic reclaim)
- `SIMPLEAGENT_QUEUE_MAX_ATTEMPTS` (default: `5`; max processing attempts before dead-letter)
- `SIMPLEAGENT_QUEUE_RETRY_BASE_S` (default: `2`; exponential retry backoff base in seconds)
- `SIMPLEAGENT_QUEUE_RETRY_CAP_S` (default: `60`; max retry backoff in seconds)
- `SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER` (default: `20`; inbound ready+processing load target per executor worker)
- `SIMPLEAGENT_AUTOSCALE_OUTBOUND_TARGET_READY_PER_WORKER` (default: `40`; outbound ready+processing load target per delivery worker)
- `SIMPLEAGENT_AUTOSCALE_EXECUTOR_MIN_WORKERS` / `SIMPLEAGENT_AUTOSCALE_EXECUTOR_MAX_WORKERS` (defaults: `0`/`100`)
- `SIMPLEAGENT_AUTOSCALE_DELIVERY_MIN_WORKERS` / `SIMPLEAGENT_AUTOSCALE_DELIVERY_MAX_WORKERS` (defaults: `0`/`100`)
- `SIMPLEAGENT_AUTOSCALE_MAX_STEP_UP` / `SIMPLEAGENT_AUTOSCALE_MAX_STEP_DOWN` (defaults: `10`/`10`; caps recommendation movement per decision)
- `OPENAI_API_KEY` (primary key used for OpenAI models)
- `ANTHROPIC_API_KEY` (used for Anthropic models)
- `GOOGLE_API_KEY` (used for Google/Gemini models)
- `SIMPLEAGENT_LLM_API_KEY` (fallback key name for OpenAI models; `ADMINAGENT_LLM_API_KEY` also accepted for migration)
- `SIMPLEAGENT_DB_PATH` (default: `simpleagent.db`, local SQLite file for session memory)
- `SIMPLEAGENT_SESSION_MAX_MESSAGES` (default: `100`, per-session retained messages)
- `SIMPLEAGENT_SYSTEM_PROMPT` (optional system prompt)
- `SIMPLEAGENT_SHELL_ENABLED` (default: `0`, enable model-requested shell commands; keep disabled for cloud multi-tenant deployments unless explicitly needed)
- `SIMPLEAGENT_WEB_ENABLED` (default: `1`, enable built-in `web_search` and `web_fetch` tools)
- `SIMPLEAGENT_WEB_MAX_CHARS` (default: `6000`, max extracted Markdown chars returned by `web_fetch`)
- `SIMPLEAGENT_SHELL_CWD` (default: `.`, working directory for shell commands)
- `SIMPLEAGENT_SHELL_TIMEOUT_S` (default: `20`, per-command timeout in seconds)
- `SIMPLEAGENT_SHELL_MAX_OUTPUT_CHARS` (default: `8000`, max chars kept from stdout/stderr)
- `SIMPLEAGENT_SHELL_MAX_CALLS_PER_TURN` (default: `3`, max shell commands per `/api/chat` turn)
- `SIMPLEAGENT_FORWARD_ENABLED` (default: `0`)
- `SIMPLEAGENT_FORWARD_URL` (optional downstream endpoint)
- `SIMPLEAGENT_FORWARD_TOKEN` (optional Bearer token for downstream forwarding)
- `SIMPLEAGENT_PUBLIC_BASE_URL` (optional public URL used by `get_callback_url()`; example `https://agent.example.com`)
- `SIMPLEAGENT_TELEGRAM_POLL_TIMEOUT_S` (default: `25`, long poll timeout for `getUpdates`)
- `SIMPLEAGENT_TELEGRAM_POLL_RETRY_S` (default: `5`, retry delay after polling errors)
- Telegram token is configured per BOT via `/api/bots/<bot_id>/config` (or the web UI), not globally.

## Endpoints

- `GET /health`
- `GET /livez` and `GET /readyz` (health probes)
- `GET /api/livez` and `GET /api/readyz` (API aliases for probes)
- `GET /api/events`
- `POST /api/chat` (chat API with `session_id` + `message`)
- `GET /api/sessions` (list known sessions from local DB)
- `GET /api/sessions/<session_id>` (view persisted history for a session)
- `POST /api/config/telegram` (update a bot's `telegram_bot_token` via API)
- `POST /hooks/outward_inbox` (record external notifications into a session)
- `GET /api/queue/stats` (inbound/outbound queue visibility)
- `POST /api/queue/process-once` (executor-plane manual drain endpoint)
- `GET /api/queue/dead-letter` (inspect failed events promoted to dead-letter)
- `POST /api/queue/dead-letter/replay` (executor-plane replay of dead-letter events)
- `POST /api/queue/dead-letter/purge` (executor-plane purge for dead-letter cleanup)
- `GET /api/metrics` (queue lag + per-plane metrics for autoscaling signals)
- `GET /api/autoscale/signals` (suggested executor/delivery worker counts + BOT_ID hot spots)
- `GET|POST /api/autoscale/recommendation` (returns `scale_up`/`scale_down`/`hold` recommendations from current worker counts)

The chat UI at `/` uses `/api/chat` and displays current model/forward config from `/health`.
Ops UI is available at `/ops` for queue/dead-letter/autoscale testing.

## Multi-User Architecture Plan

See [docs/MULTI_USER_ARCHITECTURE_PLAN.md](docs/MULTI_USER_ARCHITECTURE_PLAN.md) for the BOT_ID-based separation between:
- Gateway
- Telegram poller
- Executor
- Database
- MCP servers

Operational playbook for split runtime incidents:
- [docs/SPLIT_RUNTIME_RUNBOOK.md](docs/SPLIT_RUNTIME_RUNBOOK.md)

## Split Deployment Modes

Use a shared database and run separate processes:

1) Gateway:
- `SIMPLEAGENT_SERVICE_MODE=gateway`
- Handles ingress (`/api/chat`, Telegram inbound/webhook, external hooks)
- Queues inbound events with `bot_id`

2) Executor:
- `SIMPLEAGENT_SERVICE_MODE=executor`
- Set `SIMPLEAGENT_EXECUTOR_AUTO_RUN=1`
- Set `SIMPLEAGENT_DELIVERY_AUTO_RUN=1`
- Consumes inbound queue, runs inference/tools, writes sessions, drains outbound queue

3) Telegram poller (optional, can run on gateway process):
- `SIMPLEAGENT_TELEGRAM_POLLER_ENABLED=1`
- Polls tokens and enqueues Telegram events; no inference in poller path

### Process Entrypoints

- `python run_gateway.py`: gateway plane HTTP service (`SIMPLEAGENT_SERVICE_MODE=gateway`)
- `python run_executor.py`: executor + outbound delivery workers (`SIMPLEAGENT_SERVICE_MODE=executor`)
- `python run_telegram_poller.py`: dedicated Telegram poller worker (gateway mode, no HTTP server)

All processes should share the same `SIMPLEAGENT_DB_PATH`.

### Split Compose Example

Use the provided split compose file:

```bash
docker compose -f docker-compose.split.yml --env-file .env up -d --build
```

This starts:
- `simpleagent-gateway` (HTTP ingress)
- `simpleagent-executor` (inference + tool orchestration + outbound delivery)
- `simpleagent-telegram-poller` (Telegram polling only)

### Synthetic Queue Burst

Use the helper script to generate queued chat load and observe autoscale signals:

```bash
python scripts/queue_burst.py --base-url http://127.0.0.1:18789 --requests 300 --concurrency 30 --sessions 30
```

This prints JSON lines with enqueue throughput, `/api/autoscale/signals`, and `/api/queue/stats`.

## Telegram Setup

1) Create a bot via `/api/bots` (or from the web UI at `/`).
2) Set that bot's `telegram_bot_token` via `/api/bots/<bot_id>/config`.
3) Enable polling where needed with `SIMPLEAGENT_TELEGRAM_POLLER_ENABLED=1`.
4) Start SimpleAgent and send a text message to your bot in Telegram.

No public webhook URL is needed in long polling mode.

SimpleAgent maps each Telegram chat to a session id in the form `telegram:<chat_id>`.

## Chat Request Example

```json
{
  "bot_id": "bot-123",
  "session_id": "local-dev",
  "message": "hello"
}
```
