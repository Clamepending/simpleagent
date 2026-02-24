# AdminAgent

AdminAgent is a local-first AI chat service.

It receives chat messages, keeps per-session history in memory, generates responses from an
OpenAI-compatible endpoint, and can optionally forward assistant replies downstream.

## Run Locally (recommended for fast iteration)

`app.py` auto-loads `.env`, so local testing works without manual export.

1) Create/update `.env`:

```bash
PORT=18789
ADMINAGENT_LLM_URL=http://127.0.0.1:11434/v1/chat/completions
ADMINAGENT_MODEL=llama3.2
ADMINAGENT_LLM_API_KEY=replace-with-your-openai-api-key
ADMINAGENT_FORWARD_ENABLED=0
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
For a local model on your laptop, `ADMINAGENT_LLM_URL` should also use `host.docker.internal`.

## Environment

- `PORT` (default: `18789`)
- `ADMINAGENT_LLM_URL` (OpenAI-compatible chat completions endpoint)
- `ADMINAGENT_MODEL` (model name sent to LLM endpoint)
- `ADMINAGENT_LLM_API_KEY` (Bearer token for LLM endpoint)
- `ADMINAGENT_SYSTEM_PROMPT` (optional system prompt)
- `ADMINAGENT_FORWARD_ENABLED` (default: `0`)
- `ADMINAGENT_FORWARD_URL` (optional downstream endpoint)
- `ADMINAGENT_FORWARD_TOKEN` (optional Bearer token for downstream forwarding)

## Endpoints

- `GET /health`
- `GET /api/events`
- `POST /api/chat` (chat API with `session_id` + `message`)
- `GET /api/sessions/<session_id>` (view in-memory history for a session)

The chat UI at `/` uses `/api/chat` and displays current model/forward config from `/health`.

## Chat Request Example

```json
{
  "session_id": "local-dev",
  "message": "hello"
}
```
