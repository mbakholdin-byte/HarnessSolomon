# API Reference — Solomon Harness v1.0.0+

> Last updated: 2026-06-19, v1.0.0 final. Endpoint coverage: `/api/v1/*` canonical, 10 scopes, RFC 8594 versioning.

> Все `/api/v1/*` endpoints canonical. Legacy `/api/*` paths возвращают RFC 8594 deprecation headers (`Deprecation: true`, `Sunset: Wed, 31 Dec 2026 23:59:59 GMT`, `Link: </api/v1/...>; rel="successor-version"`). После 2026-12-31 legacy paths можно переключить на 410 Gone через `legacy_apis_gone_enabled=True` (opt-in, Phase 4.12 v1.22.0).

## Базовый URL

```
http://localhost:8765
```

Dev mode (без auth): `AUTH_REQUIRED=false harness serve`.

## Аутентификация

Все `/api/v1/*` endpoints (кроме `/api/v1/capabilities`) требуют Bearer token:

```
Authorization: Bearer <plaintext-token>
```

- Missing → `401 missing Authorization header`
- Malformed → `401 invalid Authorization header`
- Unknown/revoked → `401 invalid or revoked token`
- Valid token, missing scope → `403 missing required scope: X (have: A, B)`

См. [`docs/scope-api.md`](scope-api.md) для управления токенами.

## Endpoints

### Capabilities (public)

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `GET` | `/api/v1/capabilities` | (public) | Self-description: server_version, auth_required, 10 scopes_available, live-built endpoints list |

### Health (no `/api` prefix, no auth)

| Method | Path | Описание |
|--------|------|----------|
| `GET` | `/health/live` | Liveness (always 200 unless process dead) |
| `GET` | `/health/ready` | Readiness — critical deps (Qdrant/SQLite/Neo4j) reachable. 503 если required probe failed |
| `GET` | `/health/deep` | Full diagnostics — 8 subsystem probes (Phase 4.9). 200 даже при degraded |
| `GET` | `/api/health` | (legacy alias для `/health/deep`) |
| `GET` | `/metrics` | Prometheus text format. Requires `observability_prometheus_enabled=true` |

### Sessions

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `GET` | `/api/v1/sessions?recent=N` | `sessions.read` | List recent sessions |
| `POST` | `/api/v1/sessions` | `sessions.read` | Create a session |
| `GET` | `/api/v1/sessions/{id}` | `sessions.read` | Session metadata |
| `PATCH` | `/api/v1/sessions/{id}` | `sessions.read` | Rename / change model |
| `DELETE` | `/api/v1/sessions/{id}` | `sessions.read` | Delete a session |
| `GET` | `/api/v1/sessions/{id}/messages` | `sessions.read` | Message history |
| `POST` | `/api/v1/sessions/{id}/compact` | `sessions.write` | Manual /compact (Phase 3 v1.4.0). Опц. `?bypass_cache=true` |

### Chat (WebSocket)

| Method | Path | Описание |
|--------|------|----------|
| `WS` | `/api/v1/chat/ws` | Streaming chat (tool-aware). Client→server: `{type:"user", content:"..."}`. Server→client: `{type:"token"\|"tool_call"\|"tool_result"\|"done"\|"error"}` |

### Models

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `GET` | `/api/v1/models` | (public) | Catalog моделей с `available` flag |

### Memory (Phase 1)

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `GET` | `/api/v1/memory/search?q=...` | `memory.read` | Search 4-layer memory |
| `GET` | `/api/v1/memory/stats` | `memory.read` | Memory stats |
| `POST` | `/api/v1/memory/notes` | `memory.write` | Dual-write note |

### Sub-agents (Phase 2)

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `GET` | `/api/v1/agents/jobs?recent=N` | `agents.read` | List recent sub-agent jobs |
| `POST` | `/api/v1/agents/jobs` | `agents.write` (+ `agents.pr` если `pr_mode != "off"`) | Enqueue sub-agent job (sync или background) |
| `GET` | `/api/v1/agents/jobs/{id}` | `agents.read` | Inspect single job |
| `GET` | `/api/v1/agents/health` | `agents.read` | Queue stats |

### Inbound webhooks (GitHub)

| Method | Path | Описание |
|--------|------|----------|
| `POST` | `/api/v1/agents/webhooks/github` | GitHub inbound (HMAC-SHA256 signed). Path configurable через `settings.webhook_path` |

### Elicitation (Phase 4.3-4.11)

3 транспорта (см. [`docs/elicitation.md`](elicitation.md)):

| Method | Path | Scope | Phase | Описание |
|--------|------|-------|-------|----------|
| `WS` | `/api/v1/elicitation/ws` | — | 4.3+ v1.12 | Primary transport (full-duplex). Default ON |
| `GET` | `/api/v1/elicitation/poll?session=S` | — | 4.5 v1.15 | Long-poll fallback. Opt-in (`hooks_elicitation_longpoll_enabled`) |
| `POST` | `/api/v1/elicitation/answer` | — | 4.5 v1.15 | Submit answer (long-poll companion) |
| `GET` | `/api/v1/elicitation/sse?session=S` | `elicitation.read` | 4.11 v1.21 | Server-Sent Events. Opt-in (`hooks_elicitation_sse_enabled`) |
| `GET` | `/api/v1/elicitation/history?session=S&limit=N` | — | 4.8 v1.18 | Persisted decision history (1..10000 rows) |

### Observability admin (Phase 4.11)

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `GET` | `/api/v1/observability/metrics` | `observability.read` | JSON snapshot всех counters/gauges (опц. `?filter=<regex>`) |
| `GET` | `/api/v1/observability/health/deep` | `observability.read` | JSON deep health (8 probes) |
| `GET` | `/api/v1/observability/audit/recent?limit=N` | `observability.read` | Recent HookAuditSink entries (default 50, max 500) |

### Webhook admin (Phase 4.13B)

| Method | Path | Scope | Описание |
|--------|------|-------|----------|
| `POST` | `/api/v1/webhooks/enable?url=...` | `webhooks.admin` | Re-enable auto-disabled outbound URL |
| `GET` | `/api/v1/observability/webhooks/dlq?limit=N&include_replayed=bool` | `observability.read` | List outbound webhook DLQ entries |
| `POST` | `/api/v1/observability/webhooks/dlq/{dlq_id}/replay` | `webhooks.admin` | Re-send DLQ entry с CURRENT signing secret |

См. [`docs/webhooks.md`](webhooks.md) для outbound hardening.

## HTTP status codes

| Code | When |
|------|------|
| `200` | Success |
| `201` | Resource created (POST `/api/v1/memory/notes`, POST `/api/v1/agents/jobs`) |
| `401` | Auth missing / malformed / invalid / revoked |
| `403` | Auth valid but missing scope (или feature disabled — e.g. longpoll/SSE disabled) |
| `404` | Resource not found (job_id, session_id, DLQ id, pending question) |
| `410` | Legacy `/api/*` after `legacy_apis_gone_enabled=True` (Phase 4.12) |
| `422` | Pydantic validation failed |
| `500` | Internal server error |
| `503` | Lifespan init failed (job_store/token_store/merge_queue/webhook_event_store missing) |

## API versioning policy

- **Legacy `/api/*`** (Phase 0): dual-mounted с canonical `/api/v1/*`. Deprecation headers добавляются на каждый response.
- **Canonical `/api/v1/*`** (Phase 4.1+ v1.7.2): единственный namespace для новых endpoints.
- **Sunset date:** `Wed, 31 Dec 2026 23:59:59 GMT`. После этой даты legacy paths можно переключить на 410 Gone через `legacy_apis_gone_enabled=True`.
- **WebSocket:** legacy `/api/chat/ws` остаётся dual-mounted (no deprecation headers на WS upgrade).

См. RFC 8594 (Sunset Header) и RFC 8288 (Web Linking) для стандарта.

## Examples

### Create session + chat

```bash
TOKEN="YsVQ3gfLHK..."
BASE="http://localhost:8765"

# Create session
curl -sX POST "$BASE/api/v1/sessions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"MiniMax-M2.7"}' | jq
# {"id": "abc-123", "model": "MiniMax-M2.7", ...}

# Chat (use wscat or your WS client)
wscat -c "$BASE/api/v1/chat/ws" \
  -H "Authorization: Bearer $TOKEN"
> {"type":"user","content":"Hello"}
< {"type":"token","content":"Hi"}
< {"type":"done"}
```

### Trigger compaction

```bash
curl -sX POST "$BASE/api/v1/sessions/abc-123/compact?bypass_cache=true" \
  -H "Authorization: Bearer $TOKEN" | jq
# {"saved_tokens": 4500, "original_tokens": 9000, ...}
```

### Subscribe to SSE elicitation

```bash
curl -N "$BASE/api/v1/elicitation/sse?session=abc-123" \
  -H "Authorization: Bearer $TOKEN_WITH_ELICITATION_READ"
# event: new_question
# data: {"question_id":"...","question":"Delete file X?","options":["yes","no"],...}
#
# : keep-alive
```

### Scrape metrics

```bash
# Prometheus text format
curl -s "$BASE/metrics" | grep "harness_llm"
# # HELP harness_llm_calls_total ...
# harness_llm_calls_total{model="MiniMax-M2.7",tier="T3",status="ok"} 42.0

# JSON admin snapshot (требует observability.read scope)
curl -s "$BASE/api/v1/observability/metrics?filter=harness_llm" \
  -H "Authorization: Bearer $TOKEN_WITH_OBS_READ" | jq
```

### Replay DLQ entry

```bash
# List DLQ
curl -s "$BASE/api/v1/observability/webhooks/dlq?limit=10" \
  -H "Authorization: Bearer $TOKEN_WITH_OBS_READ" | jq
# {"entries": [{"id": 5, "url": "https://...", ...}], "count": 1, ...}

# Replay entry 5
curl -sX POST "$BASE/api/v1/observability/webhooks/dlq/5/replay" \
  -H "Authorization: Bearer $TOKEN_WITH_WEBHOOK_ADMIN" | jq
# {"dlq_id": 5, "replayed": true, "status_code": 200}
```

## См. также

- [`docs/scope-api.md`](scope-api.md) — 10 RBAC scopes, token lifecycle
- [`docs/elicitation.md`](elicitation.md) — Elicitation 3 транспорта
- [`docs/webhooks.md`](webhooks.md) — Outbound webhook hardening
- [`docs/observability.md`](observability.md) — Observability framework
- [`docs/cli.md`](cli.md) — CLI subcommands
- `harness/server/routes/` — исходный код всех endpoints

---

**Версия документа:** v1.22.0 (2026-06-19)
