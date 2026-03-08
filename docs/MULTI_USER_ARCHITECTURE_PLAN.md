# Multi-User Cloud Architecture Plan (BOT_ID)

This plan enforces hard separation between `gateway`, `telegram poller`, `executor`, `database`, and `MCP servers`, while keeping per-user cost low via sparse/event-driven execution.

## 1) Service Boundaries

### Gateway (Ingress + Delivery Router)
Responsibilities:
- Accept all external inbound traffic.
- Authenticate requests and validate payload shape.
- Resolve/validate `bot_id` and write inbound events to queue/storage.
- Never run LLM inference.
- Never call MCP directly.

Endpoints owned by gateway:
- Telegram inbound webhook/poller handoff.
- UI/API ingress (`/api/chat` style requests can enter here and be queued).
- Outbound delivery routing (Telegram send, webhooks, etc.).

Contract:
- Every inbound event must contain `bot_id` (except bot creation flows).
- Add `event_id`, `trace_id`, and `idempotency_key`.

### Telegram Poller (Source Adapter)
Responsibilities:
- Poll Telegram APIs for assigned bot tokens.
- Convert each update to normalized gateway event format.
- Forward to gateway with attached `bot_id`.
- No LLM/MCP/database business logic beyond checkpoint state.

Scaling:
- Shard by token hash (`token_hash % shard_count`).
- Keep offsets/checkpoints in DB so pollers are stateless and replaceable.

### Executor (Inference + Orchestration)
Responsibilities:
- Consume queued events from gateway.
- Load bot config/context (`SOUL.md`, `USER.md`, `HEARTBEAT.md`).
- Run inference.
- Execute tools (MCP/gateway tool calls) with `bot_id` propagation.
- Persist assistant messages and enqueue outbound actions.

Non-responsibilities:
- No direct public ingress.
- No Telegram polling.

Scaling:
- Horizontal scale by queue depth.
- Keep workers stateless; use DB/cache for state.

### Database (Source of Truth)
Responsibilities:
- Persistent bot config, ownership, sessions, message history, queue/outbox state.
- Enforce constraints (unique token mapping, immutable Telegram owner rule).
- Support idempotency and replay safety.

Core constraints:
- Unique non-empty Telegram token across bots.
- First Telegram sender claim is permanent owner for that bot.
- All session/message rows scoped by `bot_id`.

### MCP Servers (Capability Plane)
Responsibilities:
- Execute tool actions only.
- Treat `bot_id` as required tenant context.
- Return tool result without cross-tenant state leakage.

Contract:
- All MCP calls carry `bot_id` (and optionally `_bot_id` internal alias).
- MCP must reject requests missing tenant context.

## 2) BOT_ID-First Message Contracts

All internal envelopes:

```json
{
  "event_id": "evt_x",
  "trace_id": "tr_x",
  "idempotency_key": "idem_x",
  "bot_id": "bot_x",
  "session_id": "telegram:12345",
  "source": "telegram|ui|hook",
  "type": "user_message|tool_result|heartbeat",
  "payload": {}
}
```

Rules:
- `bot_id` mandatory after bot creation.
- `session_id` unique only within `(bot_id, session_id)` scope.
- Queue consumers must dedupe on `idempotency_key`.

## 3) Cheap Sparse Scaling Strategy

### Event-Driven Execution
- No per-user always-on process.
- Users appear “always on” because gateway always accepts input quickly and schedules execution.

### Queue + Outbox Pattern
- Inbound queue: gateway -> executor.
- Outbox queue: executor -> delivery workers.
- Retries with backoff and dead-letter queue for failures.

### Memory Cost Control
- Keep bounded per-session history window.
- Periodically summarize long sessions into compact memory blocks.
- Cache hot bot config/context with short TTL (e.g., 30-120s).

### Compute Cost Control
- Dynamic model routing per bot (cheap default, escalate only when needed).
- Tool-only responses when possible to avoid extra model turns.
- Concurrency and per-bot rate limits to prevent noisy-neighbor spikes.

## 4) OpenClaw-Like Behavior (Without Shell/Filesystem Focus)

Capabilities preserved:
- Persistent persona (`SOUL.md`), user profile (`USER.md`), runtime cadence (`HEARTBEAT.md`).
- Tool orchestration with strict tenant context.
- Heartbeat cadence adjustable per bot.

Not in current scope:
- Filesystem and shell command tools as first-class production features.

## 5) Security + Isolation Requirements

- Enforce first Telegram sender ownership claim transactionally:
  - If owner is null: set owner to sender.
  - Else sender must equal owner.
- Keep bot-token mapping unique in DB.
- Validate `bot_id` on every ingress and every tool/gateway call.
- Require service-to-service auth between components (signed token/JWT).

## 6) Migration Plan (Monolith -> Split Services)

### Phase A (Now)
- Keep current app but maintain strict conceptual boundaries in code paths.
- Ensure all tool/downstream payloads include `bot_id`.
- Keep Telegram owner/token constraints in DB.

### Phase B
- Extract gateway into separate service.
- Extract executor worker process from HTTP service.
- Add queue and outbox tables/transport.

### Phase C
- Extract Telegram poller into dedicated shardable workers.
- Add autoscaling triggers from queue lag and throughput.

### Phase D
- Add SLOs/observability by component:
  - queue lag, executor latency, tool latency, delivery success rate.

## 7) Non-Negotiable Invariants

1. No inference without `bot_id`.
2. No MCP call without `bot_id`.
3. No cross-bot session reads/writes.
4. Telegram owner claim is immutable after first message.
5. Duplicate Telegram token across bots is rejected.
6. Gateway must be able to accept traffic even when executors are scaled to zero (queue buffer).

