# API ↔ Configuration Cross-Reference

> **Auto-generated** on 2026-06-21 13:41
> 51 API endpoints, 233 configuration fields

## Summary by HTTP Method

| Method | Count |
|--------|-------|
| GET | 34 |
| POST | 14 |
| PUT | 1 |
| DELETE | 2 |

## All Endpoints

| Method | Path | File | Settings | Description |
|--------|------|------|----------|-------------|
| DELETE | `/sessions/{session_id}` | sessions.py | — | Delete session + cascade messages. |
| DELETE | `/zones/{zone_id}` | privacy_zones.py | — | Delete a privacy zone. |
| GET | `` | audit.py | `observability_log_dir` | List audit entries with date-range filter, pagination, and format. |
| GET | `` | sessions_v1.py | — | List the most recently updated sessions (Phase 1.6). |
| GET | `/api/health` | observability.py | — | Backward-compat alias for ``/health/deep`` (Phase 0+). |
| GET | `/audit/recent` | observability_admin.py | `hooks_observability_admin_audit_max_limit` | Return the last N :class:`HookAuditSink` entries. |
| GET | `/capabilities` | capabilities.py | `auth_required` | Return the server's self-description. |
| GET | `/health` | agents_jobs.py | — | Ops health endpoint: per-repo lock stats + recent job count. |
| GET | `/health` | health.py | `project_root` | Liveness probe. |
| GET | `/health/deep` | observability.py | — | Deep health: run every registered probe with full timeout.
Used by ops dashboard |
| GET | `/health/deep` | observability_admin.py | — | Return a JSON deep health report (8 subsystem probes). |
| GET | `/health/live` | observability.py | — | Liveness: is the process alive? Always 200 unless the Python
interpreter is brok |
| GET | `/health/ready` | observability.py | — | Readiness: are the required dependencies up? Returns 503 if
``require_qdrant`` / |
| GET | `/history` | elicitation_history.py | — | Return the persisted Elicitation decision history. |
| GET | `/hooks` | hooks_admin.py | — | List all registered hooks (builtin + custom) with on/off state. |
| GET | `/hooks/{hook_id}` | hooks_admin.py | — | Get a single hook by id. |
| GET | `/jobs` | agents_jobs.py | — | List the ``recent`` most recent jobs (default 20, newest first). |
| GET | `/jobs/{job_id}` | agents_jobs.py | — | Fetch one job by id. 404 if not found. |
| GET | `/metrics` | observability.py | — | Prometheus scrape endpoint. Disabled by default in Settings. |
| GET | `/metrics` | observability_admin.py | `hooks_observability_admin_metrics_filter` | Return a JSON snapshot of all Prometheus counters + gauges. |
| GET | `/models` | models.py | — | Return all catalog models with availability computed from env. |
| GET | `/plugins` | marketplace.py | — | List available plugins in the marketplace. |
| GET | `/plugins` | plugins_admin.py | — | List all loaded plugins with their enabled/disabled state. |
| GET | `/plugins/{name}` | marketplace.py | — | Get a single plugin manifest by name. |
| GET | `/plugins/{name}` | plugins_admin.py | — | Get a single plugin by name. |
| GET | `/poll` | elicitation_longpoll.py | — | Long-poll the broker for the next pending question. |
| GET | `/search` | memory_v1.py | — | Search the 4-layer memory with BM25 + identity rerank. |
| GET | `/sessions` | sessions.py | — | List most recent sessions. |
| GET | `/sessions/{session_id}` | sessions.py | — | Get session by id. |
| GET | `/sessions/{session_id}/messages` | sessions.py | — | List messages for a session, in order. |
| GET | `/sse` | elicitation_sse.py | — | Server-Sent Events stream for Elicitation lifecycle events. |
| GET | `/stacks/{stack_id}` | agents_jobs.py | — | Phase 2.4: fetch a stack by its ``pr_stack_id``. |
| GET | `/stats` | memory_v1.py | — | Return per-layer entry counts (cheap, best-effort). |
| GET | `/webhooks/dlq` | observability_admin.py | — | List recent outbound webhook DLQ entries (Phase 4.13B Drift 2). |
| GET | `/zones` | privacy_zones.py | — | List all REST-managed privacy zones. |
| GET | `/zones/{zone_id}` | privacy_zones.py | — | Get a single privacy zone by id. |
| POST | `/answer` | elicitation_longpoll.py | — | Submit an answer for a pending question. |
| POST | `/hooks/{hook_id}/disable` | hooks_admin.py | — | Disable a hook. |
| POST | `/hooks/{hook_id}/enable` | hooks_admin.py | — | Enable a hook. |
| POST | `/jobs` | agents_jobs.py | `pr_default_target_branch` | Enqueue a sub-agent job (Phase 1.6). |
| POST | `/notes` | memory_v1.py | — | Dual-write a new memory note to the unified facade. |
| POST | `/plugins/{name}/disable` | plugins_admin.py | — | Disable a plugin: unload + mark disabled. |
| POST | `/plugins/{name}/enable` | plugins_admin.py | — | Re-enable a previously disabled plugin. |
| POST | `/sessions` | sessions.py | — | Create new session. |
| POST | `/sessions/{session_id}/messages` | sessions.py | — | Add a message to a session. |
| POST | `/webhooks/dlq/{dlq_id}/replay` | observability_admin.py | — | Replay a single DLQ entry (Phase 4.13B Drift 2). |
| POST | `/webhooks/enable` | webhooks_admin.py | — | Re-enable an auto-disabled outbound webhook URL. |
| POST | `/webhooks/github` | agents_webhooks.py | `webhook_max_payload_kb`, `webhook_secret` | Receive a single inbound GitHub webhook event. |
| POST | `/zones` | privacy_zones.py | — | Create a new privacy zone. |
| POST | `/{session_id}/compact` | sessions_v1.py | — | Force-compact a session's context (Phase 3 v1.4.0). |
| PUT | `/zones/{zone_id}` | privacy_zones.py | — | Update an existing privacy zone. |

## Reverse Map: Settings → Endpoints

Which API endpoints use which configuration fields.

| Setting | Endpoints |
|---------|-----------|
| `auth_required` | `GET /capabilities` |
| `hooks_observability_admin_audit_max_limit` | `GET /audit/recent` |
| `hooks_observability_admin_metrics_filter` | `GET /metrics` |
| `observability_log_dir` | `GET ` |
| `pr_default_target_branch` | `POST /jobs` |
| `project_root` | `GET /health` |
| `webhook_max_payload_kb` | `POST /webhooks/github` |
| `webhook_secret` | `POST /webhooks/github` |

---

_Generated by `gen-api-config-map.py` on 2026-06-21 13:41_