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
OPENAI_API_KEY=replace-with-your-openai-api-key
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
SIMPLEAGENT_LLM_API_KEY=
SIMPLEAGENT_DB_PATH=./simpleagent.db
SIMPLEAGENT_SHELL_ENABLED=1
SIMPLEAGENT_WEB_ENABLED=1
SIMPLEAGENT_SHELL_CWD=.
SIMPLEAGENT_SHELL_TIMEOUT_S=20
SIMPLEAGENT_SHELL_MAX_OUTPUT_CHARS=8000
SIMPLEAGENT_SHELL_MAX_CALLS_PER_TURN=3
SIMPLEAGENT_FORWARD_ENABLED=0
SIMPLEAGENT_FORWARD_URL=http://127.0.0.1:8000/hooks/inbox
SIMPLEAGENT_FORWARD_TOKEN=replace-with-your-downstream-api-key
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

In Docker mode, `SIMPLEAGENT_FORWARD_URL` should usually use `host.docker.internal`.
For a local model on your laptop, `SIMPLEAGENT_LLM_URL` should also use `host.docker.internal`.

## Environment

- `PORT` (default: `18789`)
- `SIMPLEAGENT_LLM_URL` (OpenAI-compatible chat completions endpoint)
- `SIMPLEAGENT_MODEL` (model name sent to LLM endpoint)
- `OPENAI_API_KEY` (primary key used for OpenAI models)
- `ANTHROPIC_API_KEY` (used for Anthropic models)
- `GOOGLE_API_KEY` (used for Google/Gemini models)
- `SIMPLEAGENT_LLM_API_KEY` (fallback key name for OpenAI models; `ADMINAGENT_LLM_API_KEY` also accepted for migration)
- `SIMPLEAGENT_DB_PATH` (default: `simpleagent.db`, local SQLite file for session memory)
- `SIMPLEAGENT_SESSION_MAX_MESSAGES` (default: `100`, per-session retained messages)
- `SIMPLEAGENT_SYSTEM_PROMPT` (optional system prompt)
- `SIMPLEAGENT_SHELL_ENABLED` (default: `1`, enable model-requested shell commands)
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
- `POST /hooks/outward_inbox` (record external notifications into a session)

The chat UI at `/` uses `/api/chat` and displays current model/forward config from `/health`.

## Telegram Setup

1) Set `TELEGRAM_BOT_TOKEN` in `.env` (or from the web UI at `/`).
2) Keep `TELEGRAM_POLL_ENABLED=1` (default).
3) Start SimpleAgent and send a text message to your bot in Telegram.

No public webhook URL is needed in long polling mode.

SimpleAgent maps each Telegram chat to a session id in the form `telegram:<chat_id>`.

## Chat Request Example

```json
{
  "session_id": "local-dev",
  "message": "hello"
}
```
