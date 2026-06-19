# Outbound Webhooks — Solomon Harness v1.0.0+

> Last updated: 2026-06-19, v1.0.0 final. Includes Phase 4.13B hardening (auto-disable, DLQ admin, secret rotation).

> **Phase 2.5 + 4.3 + 4.8 + 4.13B** — outbound webhook delivery с HMAC-SHA256 signing, retry с exponential backoff, dead-letter queue (DLQ), auto-disable circuit breaker, secret rotation, admin endpoints.

## Контекст

Solomon Harness отправляет webhook events на outbound URLs (operator-configured через `outbound_webhook_urls`). Типичные use cases:

- CI/CD triggers на sub-agent job completion
- Slack/Teams notifications через webhook fanout (Phase 4.3 `Notification` event)
- Audit mirroring в external SIEM
- Custom integrations (e.g. update Jira ticket on PR merge)

## Endpoints (admin, Phase 4.13B v1.23)

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `POST` | `/api/v1/webhooks/enable?url=<url>` | `webhooks.admin` | Re-enable auto-disabled URL. Resets `disabled_at=NULL`, `consecutive_failures=0`. Idempotent (already-active → `enabled=false`, no error). 404 если URL unknown. |
| `GET` | `/api/v1/observability/webhooks/dlq?limit=N&include_replayed=bool` | `observability.read` | List DLQ entries (default 100, max 1000). Default: только unreplayed. |
| `POST` | `/api/v1/observability/webhooks/dlq/{dlq_id}/replay` | `webhooks.admin` | Re-send DLQ entry с CURRENT signing secret. Idempotent (already-replayed → `replayed=false`, no resend). |

CLI эквиваленты:

```bash
# List DLQ
harness observability webhooks dlq list --limit 100

# Replay entry
harness observability webhooks dlq replay 5

# Re-enable URL (через прямую curl, т.к. CLI для enable не добавлен)
curl -X POST "http://localhost:8765/api/v1/webhooks/enable?url=https://example.com/hook" \
  -H "Authorization: Bearer $TOKEN_WITH_WEBHOOK_ADMIN"
```

## Lifecycle

### Delivery flow

```
1. Trigger (MergeQueue / Notification hook / etc.)
   │
   ▼
2. OutboundWebhookDispatcher.deliver(event)
   │
   ├─ Read config row из WebhookEventStore (first delivery → INSERT)
   ├─ Check disabled_at: if set → SKIP + log
   ├─ Resolve signing secret (secret_version-aware)
   ├─ POST JSON + headers
   │
   ▼
3. Response
   ├─ 2xx → success, reset failure counter
   ├─ 4xx → permanent error → DLQ immediately
   ├─ 5xx → transient → retry (exponential backoff)
   ├─ timeout → transient → retry
   └─ network error → transient → retry
```

### Retry + DLQ (Phase 4.8 v1.18, для Notification channels)

Пер-channel retry в `notify_terminal.py`:

| Error type | Retry? | DLQ? |
|------------|--------|------|
| 5xx server error | Yes (exponential backoff) | After `max_retries` exhausted |
| Timeout | Yes | After `max_retries` exhausted |
| `OSError` (network) | Yes | After `max_retries` exhausted |
| 4xx client error | No | Immediately (permanent) |
| `ValueError` (bad payload) | No | Immediately (permanent) |
| Unknown exception | Conservative: treat as transient | After `max_retries` |

**Settings:**
- `hooks_notify_max_retries=3`
- `hooks_notify_retry_initial_delay_ms=100`
- `hooks_notify_retry_max_delay_ms=5000`

**DLQ storage:** SQLite table `notify_dlq` в `data/audit/agent-jobs.db`. Columns: `dlq_id`, `ts`, `session_id`, `severity`, `channel`, `payload_json`, `last_error`, `attempts`, `terminal` (1 если permanent error, 0 если retries exhausted).

**Metric:** `notify_dlq_total{severity, channel, terminal}` — emit'ится ВСЕГДА (даже при `hooks_notify_dlq_enabled=False`). Storage opt-in через `hooks_notify_dlq_enabled=True` (default).

### Auto-disable circuit breaker (Phase 4.13B Drift 1)

Если outbound URL последовательно падает (consecutive failures ≥ threshold), URL auto-disable'ится:

- `consecutive_failures` incremented на каждой failure.
- При достижении threshold → `disabled_at = now()` ISO timestamp.
- Последующие deliveries SKIP (log warning, no network call).
- Operator re-enables через `POST /api/v1/webhooks/enable?url=...`.

**Threshold:** hardcoded в `WebhookEventStore` (5 consecutive failures). Future: configurable via setting.

**Storage:** SQLite table `outbound_webhooks` в `data/audit/agent-jobs.db`. Columns: `url`, `consecutive_failures`, `disabled_at`, `secret_version`, `created_at`, `updated_at`. Index на `disabled_at` для быстрого lookup disabled URLs.

### Secret rotation (Phase 4.13B Drift 3)

Each outbound URL имеет `secret_version` (default 1):

- **Version 1:** legacy path. Secret = `settings.outbound_webhook_token` (env var `OUTBOUND_WEBHOOK_TOKEN`).
- **Version N > 1:** secret = env var `OUTBOUND_WEBHOOK_TOKEN_V{N}` (e.g. `OUTBOUND_WEBHOOK_TOKEN_V2`).

**Rotation workflow:**
1. Operator setает new env var: `OUTBOUND_WEBHOOK_TOKEN_V2=new-secret`.
2. Operator bumps version для URL: `store.rotate_outbound_secret(url, new_version=2)`.
3. Following deliveries используют V2 secret. Old V1 secret можно отозвать на receiver side.

**HMAC-SHA256 signature** (Phase 2.5):
```
X-Harness-Signature: sha256=<hex-hmac-of-json-body>
```

Computed с использованием secret resolved by `secret_version`. Receiver verifies signature before processing.

## RBAC

| Endpoint | Scope | Phase |
|----------|-------|-------|
| `POST /api/v1/webhooks/enable` | `webhooks.admin` | 4.13B v1.23 |
| `GET /api/v1/observability/webhooks/dlq` | `observability.read` | 4.13B v1.23 |
| `POST /api/v1/observability/webhooks/dlq/{id}/replay` | `webhooks.admin` | 4.13B v1.23 |

В open dev mode (`auth_required=False`) scope checks bypassed.

## PII safety

- DLQ entries сериализуются через `_dlq_entry_to_safe_dict()` — strips known PII fields (`question_preview`, `arguments_preview`, `prompt_preview`, `answer`, `raw_payload`) из payload перед JSON.
- Outbound payloads проходят через `harness.redaction.redact_dict` перед enqueue в DLQ.
- HMAC signature covers уже-redacted body — receiver видит тот же body что и harness отправил.

## Storage

`data/audit/agent-jobs.db` (SQLite, WAL mode):

| Table | Назначение |
|-------|-----------|
| `outbound_webhooks` | Per-URL config: `url`, `consecutive_failures`, `disabled_at`, `secret_version` |
| `notify_dlq` | DLQ entries для Notification channels |
| `webhook_events` | Inbound GitHub webhook events (Phase 2.3) |

См. `harness/agents/webhook_store.py` для schema и query methods.

## Observability

- Metric: `harness_outbound_deliveries_total{kind, status_code}` (Counter, Phase 4.1)
- Metric: `harness_notify_dlq_total{severity, channel, terminal}` (Counter, Phase 4.8)
- Emit helpers: `emit_outbound_delivery(...)`, `emit_notification_dispatched(...)`
- JSONL log: events `"outbound_delivery"`, `"notification_dispatched"`

## Settings

| Setting | Default | Описание |
|---------|---------|----------|
| `outbound_webhook_urls` | `""` | Comma-separated outbound URLs |
| `outbound_webhook_token` | `""` | Signing secret (V1, env `OUTBOUND_WEBHOOK_TOKEN`) |
| `outbound_webhook_timeout_s` | `5.0` | Per-delivery HTTP timeout |
| `outbound_webhook_max_retries` | `3` | Max retries before giving up |
| `webhook_admin_enabled` | True | Mount `/api/v1/webhooks/enable` router |
| `hooks_notify_webhook_url` | `""` | Notification channel webhook URL |
| `hooks_notify_webhook_secret` | `""` | Notification channel HMAC secret |
| `hooks_notify_webhook_timeout_s` | `5.0` | Notification channel timeout |
| `hooks_notify_max_retries` | `3` | Notification per-channel retries |
| `hooks_notify_retry_initial_delay_ms` | `100` | Initial backoff |
| `hooks_notify_retry_max_delay_ms` | `5000` | Max backoff cap |
| `hooks_notify_dlq_enabled` | True | Store DLQ entries in SQLite |

## Examples

### Rotate signing secret

```bash
# 1. Set new env var on receiver + harness server
export OUTBOUND_WEBHOOK_TOKEN_V2="new-very-secret"

# 2. Restart harness (env vars read at startup)

# 3. Bump version для URL (programmatically or via future CLI)
python -c "
import asyncio
from harness.agents.webhook_store import WebhookEventStore

async def main():
    store = WebhookEventStore('data/agent-jobs.db')
    await store.init()
    row = await store.rotate_outbound_secret('https://example.com/hook', new_version=2)
    print(f'rotated: secret_version={row.secret_version}')

asyncio.run(main())
"

# 4. Verify: next delivery uses V2 secret
```

### List + replay DLQ

```bash
TOKEN="..."
BASE="http://localhost:8765"

# List unreplayed DLQ entries
curl -s "$BASE/api/v1/observability/webhooks/dlq?limit=10" \
  -H "Authorization: Bearer $TOKEN" | jq
# {
#   "entries": [{
#     "id": 5,
#     "webhook_id": "...",
#     "url": "https://example.com/hook",
#     "event_kind": "notification",
#     "payload": {"message": "Build failed", "severity": "error"},
#     "last_error": "HTTP 500",
#     "failed_at": "2026-06-19T...",
#     "replayed_at": null,
#     "attempts": 3
#   }],
#   "count": 1,
#   "limit": 10,
#   "include_replayed": false
# }

# Replay entry 5
curl -sX POST "$BASE/api/v1/observability/webhooks/dlq/5/replay" \
  -H "Authorization: Bearer $TOKEN" | jq
# {"dlq_id": 5, "replayed": true, "status_code": 200}
```

### Re-enable auto-disabled URL

```bash
curl -sX POST "$BASE/api/v1/webhooks/enable?url=https://example.com/hook" \
  -H "Authorization: Bearer $TOKEN" | jq
# {"url": "https://example.com/hook", "enabled": true}
```

## Architecture

```
harness/agents/
├── outbound.py             # OutboundWebhookDispatcher (~470 LoC)
│                           #   - deliver(event)
│                           #   - deliver_async(event)
│                           #   - resolve_token (secret_version-aware)
│                           #   - failure tracking via store
├── webhook_store.py        # SQLite store: outbound_webhooks, webhook_events, notify_dlq
│                           #   - record_outbound_failure (increments, auto-disable)
│                           #   - enable_outbound (resets)
│                           #   - rotate_outbound_secret
│                           #   - list_dlq / get_dlq_entry / mark_dlq_replayed
├── webhook_handler.py      # Inbound GitHub webhooks (Phase 2.3)
└── notify_terminal.py      # Notification hook dispatcher (Phase 4.3+)
                            #   - stdout/webhook/desktop/slack/teams channels
                            #   - per-channel retry + DLQ
```

**Trust boundary:**
- `harness/agents/outbound.py` — stdlib + asyncio + httpx (optional).
- `harness/agents/webhook_store.py` — stdlib + sqlite3 + aiosqlite.
- `harness/server/routes/webhooks_admin.py` — stdlib + FastAPI + `harness.server.auth`. NO `harness.agents` imports (DI via `app.state.webhook_event_store`).
- `harness/server/routes/observability_admin.py` — stdlib + FastAPI + `harness.observability`. Lazy import `harness.agents.webhook_store` inside handler.

## Troubleshooting

### URL auto-disabled

- Check `consecutive_failures` в DB: `sqlite3 data/agent-jobs.db "SELECT url, consecutive_failures, disabled_at FROM outbound_webhooks"`.
- Receiver returning 5xx? Network errors? All contribute.
- Re-enable: `POST /api/v1/webhooks/enable?url=...`.

### DLQ растёт

- Receiver постоянно 500-ing или timing out.
- Check `last_error` column в DLQ entries.
- If receiver is permanently broken (4xx), DLQ entries are `terminal=1` — fix receiver config before replaying.

### Replay возвращает 200 но `replayed=false`

DLQ entry уже replayed (idempotent). `replayed_at` уже set. Pass `?include_replayed=true` в list чтобы увидеть audit history.

### Secret rotation не подхватывается

- Env var correctly named? `OUTBOUND_WEBHOOK_TOKEN_V2` (not `OUTBOUND_WEBHOOK_TOKEN_2`).
- Server restarted after env var change? Env vars read at startup.
- `secret_version` bumped в DB? Check: `sqlite3 data/agent-jobs.db "SELECT url, secret_version FROM outbound_webhooks"`.

## См. также

- [`docs/hooks.md`](hooks.md) — Notification event (hook-driven fanout)
- [`docs/api.md`](api.md) — admin endpoints reference
- [`docs/scope-api.md`](scope-api.md) — `webhooks.admin` scope
- `harness/agents/outbound.py` — dispatcher source
- `harness/agents/webhook_store.py` — SQLite store schema
- `tests/test_notify_retry_dlq.py` — retry + DLQ tests (25 tests)
- `tests/test_webhooks_admin_*.py` — admin endpoint tests

---

**Версия документа:** v1.23.0 (2026-06-19)
**Phase:** 2.5 + 4.3 + 4.8 + 4.13B — Outbound webhook hardening
