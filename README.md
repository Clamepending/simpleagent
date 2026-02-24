# AdminAgent

AdminAgent is a minimal OpenClaw-style gateway service for webhook integrations.

It receives webhook events through a generic inbox, records them for inspection, and can
optionally forward a normalized payload to another downstream agent endpoint.

## Run Locally (recommended for fast iteration)

`app.py` auto-loads `.env`, so local testing works without manual export.

1) Create/update `.env`:

```bash
PORT=18789
GATEWAY_TOKEN=change-me
ADMINAGENT_FORWARD_ENABLED=1
ADMINAGENT_FORWARD_URL=http://127.0.0.1:8000/hooks/inbox
ADMINAGENT_FORWARD_TOKEN=replace-with-your-downstream-api-key
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

In Docker mode, `ADMINAGENT_FORWARD_URL` should usually use `host.docker.internal`.

## Environment

- `PORT` (default: `18789`)
- `GATEWAY_TOKEN` (optional Bearer token required for incoming hooks)
- `GATEWAY_HOOK_PATH` (default: `inbox`)
- `GATEWAY_MESSAGE_TEMPLATE` (optional message template with `{{field}}` placeholders)
- `ADMINAGENT_FORWARD_ENABLED` (default: `0`)
- `ADMINAGENT_FORWARD_URL` (optional downstream endpoint)
- `ADMINAGENT_FORWARD_TOKEN` (optional Bearer token for downstream forwarding)
- `DISCORD_WEBHOOK_URL` (optional, enables `/api/actions/discord`)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (optional, enables `/api/actions/telegram`)

## Endpoints

- `GET /health`
- `GET /api/events`
- `POST /api/trigger` (manual trigger for tests)
- `POST /hooks/<configured-path>` (default: `/hooks/inbox`)
- `POST /api/actions/discord`
- `POST /api/actions/telegram`

The chat UI at `/` displays current hook path, forward URL, and configured keys from `/health`.

## Payload accepted by inbox

Expected fields (example):

```json
{
  "source": "videomemory",
  "event_type": "task_update",
  "task_id": "0",
  "io_id": "net0",
  "task_description": "Count people entering room",
  "note": "Detected one person",
  "task_status": "active",
  "task_done": false
}
```
