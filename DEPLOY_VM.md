# AdminAgent VM Deployment (Minimal)

This guide deploys AdminAgent on a single Linux VM with Docker Compose and persistent local SQLite storage.

## 1) VM prerequisites

- Ubuntu/Debian VM with SSH access
- Docker + Docker Compose plugin installed
- A domain name (optional, only if you want HTTPS/public access)

## 2) Get the code

```bash
sudo mkdir -p /opt/adminagent
sudo chown "$USER":"$USER" /opt/adminagent
git clone <YOUR_REPO_URL> /opt/adminagent
cd /opt/adminagent
```

## 3) Create env file

Create `/opt/adminagent/.env`:

```env
PORT=18789
ADMINAGENT_LLM_URL=https://api.openai.com/v1/chat/completions
ADMINAGENT_LLM_API_KEY=<your-key>
ADMINAGENT_MODEL=gpt-4o-mini

# Persist session memory on VM disk
ADMINAGENT_DB_PATH=/data/adminagent.db
ADMINAGENT_SESSION_MAX_MESSAGES=100

# Optional Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_POLL_ENABLED=1
TELEGRAM_POLL_TIMEOUT_S=25
TELEGRAM_POLL_RETRY_S=5

# Optional forwarding
ADMINAGENT_FORWARD_ENABLED=0
ADMINAGENT_FORWARD_URL=
ADMINAGENT_FORWARD_TOKEN=
```

## 4) Add persistent volume + localhost bind

Edit `/opt/adminagent/docker-compose.yml` so service `adminagent` includes:

```yaml
services:
  adminagent:
    ports:
      - "127.0.0.1:18789:18789"
    volumes:
      - /opt/adminagent/data:/data
```

Then create data directory:

```bash
mkdir -p /opt/adminagent/data
```

## 5) Start

```bash
cd /opt/adminagent
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
cd /opt/adminagent
git pull
docker compose --env-file .env up -d --build
```

SQLite session data stays in `/opt/adminagent/data/adminagent.db`.

## 8) Backups (recommended)

Backup at least:

- `/opt/adminagent/.env`
- `/opt/adminagent/data/adminagent.db`

Example:

```bash
tar -czf /opt/adminagent-backup-$(date +%F).tgz /opt/adminagent/.env /opt/adminagent/data/adminagent.db
```

