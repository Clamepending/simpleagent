# SimpleAgent VM Deployment (Minimal)

This guide deploys SimpleAgent on a single Linux VM with Docker Compose and persistent local SQLite storage.

## 1) VM prerequisites

- Ubuntu/Debian VM with SSH access
- Docker + Docker Compose plugin installed
- A domain name (optional, only if you want HTTPS/public access)

## 2) Get the code

```bash
sudo mkdir -p /opt/simpleagent
sudo chown "$USER":"$USER" /opt/simpleagent
git clone <YOUR_REPO_URL> /opt/simpleagent
cd /opt/simpleagent
```

## 3) Create env file

Create `/opt/simpleagent/.env`:

```env
PORT=18789
SIMPLEAGENT_LLM_URL=https://api.openai.com/v1/chat/completions
SIMPLEAGENT_LLM_API_KEY=<your-key>
SIMPLEAGENT_MODEL=gpt-4o-mini

# Persist session memory on VM disk
SIMPLEAGENT_DB_PATH=/data/simpleagent.db
SIMPLEAGENT_SESSION_MAX_MESSAGES=100

# Optional Telegram
SIMPLEAGENT_TELEGRAM_POLLER_ENABLED=1
SIMPLEAGENT_TELEGRAM_POLL_TIMEOUT_S=25
SIMPLEAGENT_TELEGRAM_POLL_RETRY_S=5

# Queue retry policy (executor/delivery workers)
SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S=120
SIMPLEAGENT_QUEUE_MAX_ATTEMPTS=5
SIMPLEAGENT_QUEUE_RETRY_BASE_S=2
SIMPLEAGENT_QUEUE_RETRY_CAP_S=60

# Autoscale tuning signals (used by /api/autoscale/signals)
SIMPLEAGENT_AUTOSCALE_INBOUND_TARGET_READY_PER_WORKER=20
SIMPLEAGENT_AUTOSCALE_OUTBOUND_TARGET_READY_PER_WORKER=40
SIMPLEAGENT_AUTOSCALE_EXECUTOR_MIN_WORKERS=0
SIMPLEAGENT_AUTOSCALE_EXECUTOR_MAX_WORKERS=100
SIMPLEAGENT_AUTOSCALE_DELIVERY_MIN_WORKERS=0
SIMPLEAGENT_AUTOSCALE_DELIVERY_MAX_WORKERS=100
SIMPLEAGENT_AUTOSCALE_MAX_STEP_UP=10
SIMPLEAGENT_AUTOSCALE_MAX_STEP_DOWN=10

# Optional forwarding
SIMPLEAGENT_FORWARD_ENABLED=0
SIMPLEAGENT_FORWARD_URL=
SIMPLEAGENT_FORWARD_TOKEN=
```

Telegram tokens are linked per BOT at runtime via `/api/bots/<bot_id>/config` (or the web UI).

## 4) Add persistent volume + localhost bind

Edit `/opt/simpleagent/docker-compose.yml` so service `simpleagent` includes:

```yaml
services:
  simpleagent:
    ports:
      - "127.0.0.1:18789:18789"
    volumes:
      - /opt/simpleagent/data:/data
```

Then create data directory:

```bash
mkdir -p /opt/simpleagent/data
```

## 5) Start

```bash
cd /opt/simpleagent
docker compose --env-file .env up -d --build
```

### Optional: split processes (recommended for scale)

```bash
cd /opt/simpleagent
docker compose -f docker-compose.split.yml --env-file .env up -d --build
```

This runs:
- gateway HTTP service
- executor worker (inference + outbound delivery)
- telegram poller worker

## 6) Verify

```bash
docker compose ps
curl -s http://127.0.0.1:18789/health
curl -s http://127.0.0.1:18789/readyz
curl -s http://127.0.0.1:18789/api/metrics
curl -s http://127.0.0.1:18789/api/autoscale/signals
curl -s "http://127.0.0.1:18789/api/autoscale/recommendation?current_executor_workers=2&current_delivery_workers=1"
curl -s http://127.0.0.1:18789/api/queue/dead-letter
curl -s -X POST http://127.0.0.1:18789/api/queue/dead-letter/replay -H 'Content-Type: application/json' -d '{"queue":"inbound","bot_id":"bot-abc"}'
curl -s -X POST http://127.0.0.1:18789/api/queue/dead-letter/purge -H 'Content-Type: application/json' -d '{"queue":"inbound","bot_id":"bot-abc","limit":100}'
```

You should see `"status":"ok"` in health output.

## 7) Updates

```bash
cd /opt/simpleagent
git pull
docker compose --env-file .env up -d --build
```

SQLite session data stays in `/opt/simpleagent/data/simpleagent.db`.

## 8) Backups (recommended)

Backup at least:

- `/opt/simpleagent/.env`
- `/opt/simpleagent/data/simpleagent.db`

Example:

```bash
tar -czf /opt/simpleagent-backup-$(date +%F).tgz /opt/simpleagent/.env /opt/simpleagent/data/simpleagent.db
```
