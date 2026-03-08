# Split Runtime Runbook (Gateway / Executor / Telegram Poller)

This runbook is for operating split mode with strict BOT_ID tenant separation.

## Roles

- `gateway`: ingress only (`/api/chat`, telegram inbound/webhook, hooks), queue write, no inference.
- `executor`: queue drain, inference, MCP/tool execution, session writes, outbound queue drain.
- `telegram-poller`: Telegram `getUpdates` adapter, queue write only.
- `database`: source of truth for bots, ownership, sessions, queues.

## Baseline Health Checks

1. `curl -s http://127.0.0.1:18789/health`
2. `curl -s http://127.0.0.1:18789/readyz`
3. `curl -s http://127.0.0.1:18789/api/queue/stats`
4. `curl -s http://127.0.0.1:18789/api/metrics`
5. `curl -s http://127.0.0.1:18789/api/autoscale/signals`
6. `curl -s "http://127.0.0.1:18789/api/autoscale/recommendation?current_executor_workers=2&current_delivery_workers=1"`

Expected:
- `gateway` plane enabled on gateway process.
- `executor` plane enabled on executor process.
- low/zero `dead_letter` counts.
- queue `queued_ready` drains over time.

## Incident: Inbound Queue Growing

Symptoms:
- `/api/metrics` inbound `queued_ready` increasing.
- `/api/autoscale/signals` inbound `suggested_workers` > current executor workers.

Actions:
1. Scale executor replicas up.
2. Check LLM/tool dependency health.
3. Confirm no BOT_ID hot spot from `hot_bots`.
4. Verify stale lock recovery threshold: `SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S`.
5. Use `/api/autoscale/recommendation` for bounded step action guidance.

## Incident: Outbound Queue Growing

Symptoms:
- outbound `queued_ready` increasing.
- outbound `suggested_workers` > current delivery workers.

Actions:
1. Scale executor/delivery workers up.
2. Verify downstream endpoints (Telegram/forward webhook).
3. Check dead-letter for persistent downstream failures.

## Incident: Dead-Letter Build-Up

Inspect:

```bash
curl -s "http://127.0.0.1:18789/api/queue/dead-letter?queue=both&limit=100"
```

Replay once dependencies recover:

```bash
curl -s -X POST http://127.0.0.1:18789/api/queue/dead-letter/replay \
  -H 'Content-Type: application/json' \
  -d '{"queue":"both","bot_id":"bot-abc","reset_attempts":false}'
```

Notes:
- replay endpoint is executor-plane only.
- keep `reset_attempts=false` by default to preserve retry history and avoid duplicate-user-message edge cases.

Purge old dead-letter records after triage:

```bash
curl -s -X POST http://127.0.0.1:18789/api/queue/dead-letter/purge \
  -H 'Content-Type: application/json' \
  -d '{"queue":"both","bot_id":"bot-abc","limit":500}'
```

## Incident: Worker Crash Mid-Event

Recovery behavior:
- stuck `processing` rows are automatically re-queued after `SIMPLEAGENT_QUEUE_LOCK_TIMEOUT_S`.

Tune:
- lower timeout for faster recovery.
- raise timeout if false reclaims occur on slow jobs.

## Security/Isolation Checks

1. every ingress/tool payload includes `bot_id`.
2. duplicate Telegram token across bots rejected.
3. Telegram owner claim immutable after first sender.
4. `/api/mcp/call` is executor-plane only.
