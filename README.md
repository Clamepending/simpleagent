# AdminAgent

AdminAgent is a local-first AI chat service.

It receives chat messages, keeps per-session history in a local SQLite database, generates responses from an
OpenAI-compatible endpoint, and can optionally forward assistant replies downstream.

## Run Locally (recommended for fast iteration)

`app.py` auto-loads `.env`, so local testing works without manual export.

1) Create/update `.env`:

```bash
PORT=18789
ADMINAGENT_LLM_URL=http://127.0.0.1:11434/v1/chat/completions
ADMINAGENT_MODEL=llama3.2
OPENAI_API_KEY=replace-with-your-openai-api-key
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
ADMINAGENT_LLM_API_KEY=
ADMINAGENT_DB_PATH=./adminagent.db
ADMINAGENT_SHELL_ENABLED=1
ADMINAGENT_WEB_ENABLED=1
ADMINAGENT_SHELL_CWD=.
ADMINAGENT_SHELL_TIMEOUT_S=20
ADMINAGENT_SHELL_MAX_OUTPUT_CHARS=8000
ADMINAGENT_SHELL_MAX_CALLS_PER_TURN=3
ADMINAGENT_FORWARD_ENABLED=0
ADMINAGENT_FORWARD_URL=http://127.0.0.1:8000/hooks/inbox
ADMINAGENT_FORWARD_TOKEN=replace-with-your-downstream-api-key
TELEGRAM_BOT_TOKEN=replace-with-your-telegram-bot-token
TELEGRAM_POLL_ENABLED=1
TELEGRAM_POLL_TIMEOUT_S=25
TELEGRAM_POLL_RETRY_S=5
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
For a local model on your laptop, `ADMINAGENT_LLM_URL` should also use `host.docker.internal`.

## Environment

- `PORT` (default: `18789`)
- `ADMINAGENT_LLM_URL` (OpenAI-compatible chat completions endpoint)
- `ADMINAGENT_MODEL` (model name sent to LLM endpoint)
- `OPENAI_API_KEY` (primary key used for OpenAI models)
- `ANTHROPIC_API_KEY` (used for Anthropic models)
- `GOOGLE_API_KEY` (used for Google/Gemini models)
- `ADMINAGENT_LLM_API_KEY` (legacy fallback key name; still supported)
- `ADMINAGENT_DB_PATH` (default: `adminagent.db`, local SQLite file for session memory)
- `ADMINAGENT_SESSION_MAX_MESSAGES` (default: `100`, per-session retained messages)
- `ADMINAGENT_SYSTEM_PROMPT` (optional system prompt)
- `ADMINAGENT_SHELL_ENABLED` (default: `1`, enable model-requested shell commands)
- `ADMINAGENT_WEB_ENABLED` (default: `1`, enable built-in `web_search` and `web_fetch` tools)
- `ADMINAGENT_SHELL_CWD` (default: `.`, working directory for shell commands)
- `ADMINAGENT_SHELL_TIMEOUT_S` (default: `20`, per-command timeout in seconds)
- `ADMINAGENT_SHELL_MAX_OUTPUT_CHARS` (default: `8000`, max chars kept from stdout/stderr)
- `ADMINAGENT_SHELL_MAX_CALLS_PER_TURN` (default: `3`, max shell commands per `/api/chat` turn)
- `ADMINAGENT_FORWARD_ENABLED` (default: `0`)
- `ADMINAGENT_FORWARD_URL` (optional downstream endpoint)
- `ADMINAGENT_FORWARD_TOKEN` (optional Bearer token for downstream forwarding)
- `TELEGRAM_BOT_TOKEN` (optional, masked in `/health`, can be set from the web UI)
- `TELEGRAM_POLL_ENABLED` (default: `1`, enables Telegram long polling worker)
- `TELEGRAM_POLL_TIMEOUT_S` (default: `25`, long poll timeout for `getUpdates`)
- `TELEGRAM_POLL_RETRY_S` (default: `5`, retry delay after polling errors)

## Endpoints

- `GET /health`
- `GET /api/events`
- `POST /api/chat` (chat API with `session_id` + `message`)
- `GET /api/sessions` (list known sessions from local DB)
- `GET /api/sessions/<session_id>` (view persisted history for a session)
- `POST /api/config/telegram` (update `TELEGRAM_BOT_TOKEN` from UI/API)

The chat UI at `/` uses `/api/chat` and displays current model/forward config from `/health`.

## Telegram Setup

1) Set `TELEGRAM_BOT_TOKEN` in `.env` (or from the web UI at `/`).
2) Keep `TELEGRAM_POLL_ENABLED=1` (default).
3) Start AdminAgent and send a text message to your bot in Telegram.

No public webhook URL is needed in long polling mode.

AdminAgent maps each Telegram chat to a session id in the form `telegram:<chat_id>`.

## Chat Request Example

```json
{
  "session_id": "local-dev",
  "message": "hello"
}
```
