# Migration Guide — v0.x → v1.0 — Solomon Harness

> Гайд для обновления с ранних версий (Phase 0 MVP v0.1.0) до v1.0.0 final (Phase 4.14). Описывает breaking changes, deprecated paths, новые обязательные шаги.
> Last updated: 2026-06-19, v1.0.0 final.

## TL;DR

```bash
# 1. Update
git pull
python -m pip install -e .

# 2. Existing data migrations — автоматические при старте сервера
python -m harness
# → [harness] rebuilt N sessions from JSONL (если БД пустая)
# → [harness] token_store: .../harness-scope.db (auth_required=True)

# 3. Get auth token (первым read-only CLI вызовом)
harness auth list
# → [harness] bootstrap-admin token created
# → SAVE THIS: YsVQ3gfLHK...

# 4. Update clients: /api/* → /api/v1/* (deprecated но ещё работают)
```

## Breaking changes by version

### v0.1.0 → v1.0.0 (Phase 1 — Memory + Auth)

**Auth required by default** (Phase 1.6):
- `settings.auth_required = True` (раньше implicit open).
- Все `/api/v1/*` endpoints теперь требуют Bearer token.
- `/api/*` legacy paths остаются открытыми (для backward compat).
- **Migration:** либо `AUTH_REQUIRED=false` (dev mode), либо создать токен через `harness auth list` (bootstrap-admin auto-minted).

**Memory subsystem** (Phase 1):
- New SQLite DB `data/memory.db` для UnifiedMemory.
- Legacy sessions (`data/sessions/*.jsonl`) не мигрируются автоматически — они остаются source of truth для chat history.
- **Migration:** ничего не делать. Memory начинает записывать новые факты с первого использования.

### v1.5.0 → v1.6.0 (Phase 4.0 — Hooks framework)

**Hooks framework включён по default:**
- `settings.hooks_enabled = True`.
- 5 builtin hooks активны: `log`, `validate`, `block_dangerous`, `autosave` (default ON), `inject_context` (default OFF, opt-in).
- **Migration:** ничего не делать. Hooks работают transparently. Audit log opt-in через `hooks_audit_log=True`.

**ToolRuntime changes:**
- `PreToolUse` + `PostToolUse` hooks wired.
- `block_dangerous` блокирует destructive bash commands (7 patterns). Если ваш agent использовал `rm -rf` — теперь будет blocked. Override через `PermissionRequest` hook или custom hook.
- **Migration:** проверить что agent commands не используют blocked patterns.

### v1.6.0 → v1.7.0 (Phase 4.1 — Observability framework)

**Observability включена по default (JSONL + cost tracking):**
- `observability_jsonl_enabled = True` — пишет `data/logs/harness-YYYY-MM-DD.jsonl`.
- `observability_cost_enabled = True` — per-task USD cost.
- **Migration:** ensure `data/logs/` writable. Auto-cleanup: `observability_log_max_files=30`.

**Opt-in features:**
- `observability_prometheus_enabled = False` (default). Для `/metrics` endpoint включить + `pip install prometheus-client`.
- `observability_otlp_enabled = False` (default). Для OTLP export включить + `pip install opentelemetry-*`.

### v1.7.0 → v1.7.2 (Phase 4.1+ — API versioning)

**Legacy `/api/*` deprecation:**
- Deprecation headers добавляются на каждый `/api/*` response: `Deprecation: true`, `Sunset: Wed, 31 Dec 2026 23:59:59 GMT`, `Link: </api/v1/...>; rel="successor-version"`.
- Endpoints dual-mounted: legacy `/api/*` + canonical `/api/v1/*`.
- **Migration:** обновить clients на `/api/v1/*`. Legacy paths работают до 2026-12-31.

### v1.7.2 → v1.12.0 (Phase 4.2-4.3 — Hot-reload + Elicitation)

**Hot-reload включён по default:**
- `hot_reload_enabled = True`.
- FileWatcher watches `.harness/agents/*.md`, `.harness/hooks/*.json`, `.harness/privacy/*.json`.
- **Migration:** ничего не делать. Создать `.harness/` subdirs при необходимости.

**Elicitation WebSocket:**
- New endpoint `WS /api/v1/elicitation/ws`.
- `confirm_dangerous_hook` теперь ждёт human answer (timeout 30s default).
- **Migration:** если agents зависают на 30s — подключить WS client, либо отключить через `hooks_elicitation_ws_enabled=False` (но тогда все confirmations fallback на `default_answer="abort"`).

### v1.13.0 (Phase 4.4 — CLI inspection)

**New CLI subcommands:** `harness hooks`, `harness observability`. No breaking changes.

### v1.14.0 (Phase 4.4+ — Hook wiring)

11 production trigger points wired (`safe_fire`). No breaking changes — все hooks fail-open.

### v1.15.0 (Phase 4.5 — PermissionRequest + long-poll)

**PermissionRequest hook contract:**
- `_bash` tool теперь может быть override через hook (если hook возвращает `modify` с permission_decision).
- **Migration:** если ваш denylist блокирует нужные commands — написать hook который override'ит для specific cases.

**Long-poll elicitation transport:**
- New endpoints `GET /api/v1/elicitation/poll`, `POST /api/v1/elicitation/answer`.
- Opt-in через `hooks_elicitation_longpoll_enabled=False` (default OFF).

### v1.16.0 (Phase 4.6 — Audit CLI + schemas + Slack/Teams)

**Pydantic payload schemas:**
- 16 models в `harness/hooks/schemas.py`.
- `validate_payload` fail-open: невалидный payload не блокирует dispatch, только логируется.
- **Migration:** ничего не делать.

**Slack/Teams notification channels:**
- Opt-in через env vars: `HOOKS_NOTIFY_SLACK_WEBHOOK_URL`, `HOOKS_NOTIFY_TEAMS_WEBHOOK_URL`.

### v1.17.0 (Phase 4.7 — Permission wiring + live tail)

**PermissionRequest в 5 file tools:**
- `_read_file`, `_write_file`, `_edit_file`, `_grep`, `_glob` теперь проходят через PermissionRequest hook.
- Denylists: `_READ_DENYLIST_PATTERNS` (7 patterns: `__pycache__/`, `.git/`, `.env`, `.key`, `.pem`, `secrets/`, `node_modules/`), `_WRITE_DENYLIST_PATTERNS` (superset + `.exe`, `.dll`, `.so`).
- **Migration:** если agents читали `.env` или `secrets/` — теперь blocked по default. Override через hook или убрать files из denylist.

**Live tail CLI:**
- `harness hooks audit --follow`, `harness observability metrics --follow`.

### v1.18.0 (Phase 4.8 — Elicitation history + retry/DLQ + circuit breaker)

**Defensive layer для hooks:**
- `hooks_rate_limit_enabled = True` (default). Token bucket: 60 capacity, 1.0/sec refill.
- `hooks_circuit_breaker_enabled = True` (default). Threshold 5 failures, cooldown 60s.
- **Migration:** если хуки стали "не срабатывать" — проверить `harness observability metrics --filter hook_rate_limited\|hook_circuit_skip`.

**Notify retry + DLQ:**
- `hooks_notify_dlq_enabled = True` (default).
- DLQ entries в `data/audit/agent-jobs.db`, table `notify_dlq`.

### v1.19.0 (Phase 4.9 — Per-tool/per-model metrics + deep probes)

8 deep health probes в `/health/deep`. No breaking changes.

### v1.20.0 (Phase 4.10 — Hook pattern library)

8 новых builtin hooks (pattern library). `BUILTIN_HOOKS` registry: 7 → 12. Hot-reload автоматически подхватывает `.harness/hooks/*.json`.

### v1.21.0 (Phase 4.11 — SSE + admin endpoints + 2 scopes)

**2 new RBAC scopes:**
- `observability.read` — admin observability JSON endpoints.
- `elicitation.read` — SSE elicitation transport.

**Admin endpoints (opt-in via flag, default ON):**
- `hooks_observability_admin_enabled = True`.
- `GET /api/v1/observability/{metrics,health/deep,audit/recent}`.
- **Migration:** обновить admin dashboards чтобы использовать JSON endpoints вместо парсинга Prometheus text.

**SSE transport:**
- New endpoint `GET /api/v1/elicitation/sse`.
- Opt-in через `hooks_elicitation_sse_enabled = False` (default).

### v1.22.0 (Phase 4.12 — Scratchpad perms + 410 Gone + Follower)

**PermissionRequest в scratchpad WRITE:**
- 3 метода (`_scratchpad_write_note`, `_scratchpad_plan_step`, `_scratchpad_mark_done`) теперь проходят через PermissionRequest hook.
- **Migration:** ничего не делать.

**Legacy 410 Gone (opt-in):**
- `legacy_apis_gone_enabled = False` (default). После 2026-12-31 flip в `True` → legacy `/api/*` возвращают 410 Gone с RFC 8594 headers.
- **Migration:** обновить ВСЕ clients на `/api/v1/*` ДО flip switch.

**`--follow` improvements:**
- `harness hooks audit --follow` теперь поддерживает `--batch-size N`, `--resume`, `--reset`, persistent state.

### v1.23.0 (Phase 4.13 — Webhook hardening)

**Auto-disable circuit breaker для outbound URLs:**
- 5 consecutive failures → URL auto-disabled.
- Re-enable через `POST /api/v1/webhooks/enable?url=...`.

**DLQ replay:**
- `POST /api/v1/observability/webhooks/dlq/{id}/replay`.

**Secret rotation:**
- `secret_version` column в `outbound_webhooks` table.
- Rotate через `store.rotate_outbound_secret(url, new_version=N)`. Env var `OUTBOUND_WEBHOOK_TOKEN_V{N}`.

**New RBAC scope:** `webhooks.admin` (для enable + replay mutations).

## Data migrations

Все миграции автоматические при старте сервера:

| Data | Migration | Trigger |
|------|-----------|---------|
| Sessions JSONL → SQLite | `rebuild_from_jsonl()` если БД пустая но JSONL есть | Startup |
| agent-jobs.db schema | `CREATE TABLE IF NOT EXISTS` для всех новых tables | First open |
| harness-scope.db | `TokenStore.init()` idempotent | Startup |
| Memory.db | `UnifiedMemory.__init__` creates schema | First use |

**No manual SQL migrations needed.** Alembic не используется (schema эволюционирует через `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN IF NOT EXISTS` pattern).

## Client migration checklist

Если у вас есть clients (scripts, integrations) которые используют Harness API:

- [ ] Replace `/api/health` → `/api/v1/health` (или `/health/live`)
- [ ] Replace `/api/sessions*` → `/api/v1/sessions*`
- [ ] Replace `/api/models` → `/api/v1/models`
- [ ] Replace `/api/chat/ws` → `/api/v1/chat/ws`
- [ ] Add `Authorization: Bearer <token>` header (или `AUTH_REQUIRED=false` для dev)
- [ ] For webhook receivers: verify HMAC signature (Phase 2.5+)
- [ ] For elicitation clients: consider SSE transport (Phase 4.11) вместо polling
- [ ] Subscribe to deprecation headers (`Deprecation`, `Sunset`, `Link`)

## Rollback

Solomon Harness не имеет formal rollback procedure. Если upgrade сломал что-то:

1. `git checkout <previous-tag>` (или `git revert <commit>`).
2. `python -m pip install -e .` (reinstall).
3. Restart server.

**Data compat:** SQLite schemas backward-compatible (новые columns имеют defaults, старые rows работают). JSONL logs не имеют schema (NDJSON). Token DBs (harness-scope.db) не migrated между minor versions.

## Getting help

- `harness <command> --help` — per-command help
- [`docs/quickstart.md`](quickstart.md) — setup guide
- [`docs/CHANGELOG.md`](CHANGELOG.md) — full version history
- [`docs/api.md`](api.md) — endpoints reference
- GitHub Issues: https://github.com/mbakholdin-byte/HarnessSolomon/issues

---

**Версия документа:** v1.22.0 (2026-06-19)
**Target release:** v1.0.0 final (Phase 4.14 closeout, 2026-06-19)
