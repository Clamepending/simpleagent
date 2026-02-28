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
TELEGRAM_BOT_TOKEN=
TELEGRAM_POLL_ENABLED=1
TELEGRAM_POLL_TIMEOUT_S=25
TELEGRAM_POLL_RETRY_S=5

# Optional forwarding
SIMPLEAGENT_FORWARD_ENABLED=0
SIMPLEAGENT_FORWARD_URL=
SIMPLEAGENT_FORWARD_TOKEN=
```

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

## 6) Verify

```bash
docker compose ps
curl -s http://127.0.0.1:18789/health
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

