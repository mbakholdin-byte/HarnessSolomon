# Phase 3.5 — Persistent Compact Store (v1.1.0)

> **Status:** ЗАКРЫТО v1.1.0 (2026-06-15)
> **Tag:** `v1.1.0` (annotated)
> **Tests:** 1026 mock + 5 real_llm (от 968 в v1.0.0, +58 net)

## TL;DR

Phase 3.5 расширяет Phase 3 (compaction + embeddings + privacy) persistent cache
для компакта. На cache hit — summariser LLM call skip, zero cost, instant
reconnect. Все настройки через env vars (Pydantic v2). 0 new required deps.

**Что нового:**

- `harness/agents/compact_store.py` — `CompactStore` class (SQLite)
- `harness/context/compaction_audit.py` — `CompactionAudit` (JSONL mirror)
- `CompactStore` таблица в `agent-jobs.db` (sibling `merge_jobs`/`webhook_events`)
- 3 new settings: `compaction_persistent_store`, `compaction_cache_max_versions`, `compaction_audit_log`
- Trust boundary preserved: `runner.py` continues to NOT import new modules

## Архитектура

### Persistent Compact Cache

```
┌─────────────┐     maybe_compact()     ┌──────────────────┐
│ ChatSession │ ───────────────────────> │ ContextCompactor │
│ load_history│                          │  ┌─────────────┐ │
└─────────────┘                          │  │ _source_hash│ │
                                         │  └──────┬──────┘ │
                                         │         ▼        │
                                         │  ┌─────────────┐ │
                                         │  │ lookup_cached│◄──── CompactStore
                                         │  └──────┬──────┘ │       (SQLite)
                                         │         │        │
                                  hit ◄───┤         │        │  miss
                                    │     │         ▼        │
                                    │     │  ┌─────────────┐ │
                                    │     │  │ sliding win │ │
                                    │     │  │ + summary   │ │
                                    │     │  └──────┬──────┘ │
                                    │     │         ▼        │
                                    │     │  ┌─────────────┐ │
                                    └─────┼──│ return list │ │      L2 mem0
                                          │  └─────────────┘ │  (UnifiedMemory)
                                          └──────────────────┘       │
                                                                     ▼
                                                          persist to CompactStore
                                                          (fire-and-forget)
```

**Ключевые решения:**

1. **Source hash cache key** — `(session_id, source_hash)` где
   `source_hash = sha256(json.dumps(messages, sort_keys=True))[:16]`.
   Новая история → новый hash → автоматическая cache invalidation.
2. **Failure-isolation** — cache lookup/persist failures никогда не ломают
   chat loop (fail-open). L2 mem0 + CompactStore — независимые sinks.
3. **Reconstruction** — cache хранит только summary, не полный message list.
   Reconstruct через sliding window текущих messages + cached summary.
4. **Per-session versioning** — `version = MAX(version) + 1` per session.
   No global state, no race conditions on insert.

### Storage (CompactStore)

Таблица `compact_store` в `agent-jobs.db` (sibling of `merge_jobs`):

```sql
CREATE TABLE compact_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_hash TEXT NOT NULL,          -- sha256[:16] of messages
    original_tokens INTEGER NOT NULL,
    compacted_tokens INTEGER NOT NULL,
    original_message_count INTEGER NOT NULL,
    kept_message_ids TEXT NOT NULL,     -- JSON list[int]
    summary TEXT NOT NULL,
    model TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    outcome TEXT NOT NULL,             -- 'ok' | 'fallback' | 'fail'
    created_at REAL NOT NULL,
    duration_ms REAL NOT NULL,
    UNIQUE(session_id, version)
);
CREATE INDEX idx_compact_store_session_recent
    ON compact_store(session_id, version DESC);
CREATE INDEX idx_compact_store_session_hash
    ON compact_store(session_id, source_hash);
```

**API:**

```python
class CompactStore:
    async def init() -> None
    async def lookup_cached(session_id: str, source_hash: str) -> CompactRecord | None
    async def insert(record: CompactRecord) -> int  # returns version
    async def list_for_session(session_id: str, limit: int = 10) -> list[CompactRecord]
    async def count() -> int
```

### Trust Boundary

| Module | Imports `CompactStore`? | Imports `CompactionAudit`? | Top-level? |
|--------|------------------------|---------------------------|-----------|
| `runner.py` | ❌ No | ❌ No | ❌ (verified by `test_agent_runner.py:516-575`) |
| `merge_queue.py` | ❌ No | ❌ No | ❌ |
| `outbound.py` | ❌ No | ❌ No | ❌ |
| `webhook_handler.py` | ❌ No | ❌ No | ❌ |
| `compaction.py` | ✅ (TYPE_CHECKING) | ✅ (TYPE_CHECKING) | ✅ DI param |
| `app.py` (lifespan) | ✅ (lazy import) | ✅ (lazy import) | ✅ lifespan only |

Все новые модули DI'd через `ContextCompactor.__init__` constructor. `runner.py`
продолжает использовать compactor через `compactor=compactor` kwarg (Phase 3
pattern), не видит `CompactStore` или `CompactionAudit` напрямую.

## Settings

3 new settings в `harness/config.py` секция `compaction_*`:

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `compaction_persistent_store` | `bool` | `True` | Master switch для persistent cache. False = pure in-memory (Phase 3). |
| `compaction_cache_max_versions` | `int (ge=1)` | `5` | Max cached compacts per session. Reserved для Phase 4 retention policy. |
| `compaction_audit_log` | `bool` | `False` | JSONL mirror в `data/audit/compaction-*.ndjson`. Mirrors `redaction_audit_log`. |

Plus `compaction_persistent_store=True` + `compaction_cache_max_versions < 1` →
Pydantic `ValueError` (в `_cascade_thresholds_ordered` validator).

## Quick Start

### Verify persistent cache is on

```bash
# .env
COMPACTION_PERSISTENT_STORE=true
COMPACTION_CACHE_MAX_VERSIONS=5
COMPACTION_AUDIT_LOG=false
```

### Inspect cache (SQLite CLI)

```bash
sqlite3 data/agent-jobs.db \
  "SELECT session_id, version, original_tokens, compacted_tokens, outcome, duration_ms
   FROM compact_store ORDER BY created_at DESC LIMIT 10"
```

### Enable audit log

```bash
# .env
COMPACTION_AUDIT_LOG=true
```

JSONL file location: `data/audit/compaction-YYYY-MM-DD.ndjson`

```bash
# Real-time tail
tail -f data/audit/compaction-$(date +%F).ndjson | jq .

# Cache hit rate today
jq -r 'select(.event == "cache_hit") | .event' \
  data/audit/compaction-$(date +%F).ndjson | wc -l
```

### Manual cache clear

```bash
# Drop all compacts (per session)
sqlite3 data/agent-jobs.db "DELETE FROM compact_store WHERE session_id = '...'"

# Drop everything (full reset)
sqlite3 data/agent-jobs.db "DELETE FROM compact_store"
```

## Observability

### Structured Logs

```
# Cache hit (saved LLM call)
INFO compactor.cache_hit session_id=sess-1 version=3 saved_tokens=8000 saved_ms=2.1

# Successful run (cache miss → summarise)
INFO compactor.run outcome=ok version=4 session_id=sess-1
     original_tokens=18000 compacted_tokens=900 duration_ms=4500.0

# Persist failed (warning)
WARNING compactor: persist_compact failed for session_id=sess-1: disk full
```

### JSONL Audit Schema

```json
{
  "ts": "2026-06-15T12:34:56.789Z",
  "event": "cache_hit",   // "cache_hit" | "run" | "persist_failed"
  "session_id": "sess-1",
  "version": 3,
  "saved_tokens": 8000,
  "duration_ms": 2.1
}
```

```json
{
  "ts": "2026-06-15T12:35:00.000Z",
  "event": "run",
  "session_id": "sess-2",
  "outcome": "ok",
  "version": 1,
  "original_tokens": 18000,
  "compacted_tokens": 900,
  "duration_ms": 4500.0
}
```

### Cache Hit Rate (Prometheus placeholder)

Phase 3.5 не вводит Prometheus counters — Phase 4 сделает. Пока — подсчёт из
JSONL:

```bash
# Hit rate за день
total=$(jq -r '.event' data/audit/compaction-$(date +%F).ndjson | wc -l)
hits=$(jq -r 'select(.event == "cache_hit")' data/audit/compaction-$(date +%F).ndjson | wc -l)
echo "scale=2; $hits / $total" | bc
```

## Migration from v1.0.0

Phase 3.5 **backward compatible** — `compaction_persistent_store=True` по
умолчанию, но все новые компоненты опциональны:

| v1.0.0 поведение | v1.1.0 поведение (default) | v1.1.0 override |
|------------------|----------------------------|-----------------|
| In-memory compaction | Persistent cache + L2 mirror | `compaction_persistent_store=False` |
| No audit | Structured logs only | `compaction_audit_log=True` для JSONL |
| `compactor.maybe_compact(messages, model)` | + `session_id` kwarg | callers pass positional args unchanged |

**Upgrade:**

```bash
git pull
pip install -e .  # no new deps
pytest -m "not real_llm"  # 1026 passed
git checkout v1.1.0
```

**Rollback** (если persistent cache вызывает issues):

```bash
COMPACTION_PERSISTENT_STORE=false harness serve
```

## Troubleshooting

### Cache stale (cache hit возвращает старое compaction)

**Symptom:** Compactor возвращает cached summary после ручного edit session.
**Root cause:** `source_hash` не включает manual edits, performed via direct
DB access. Cache hit → return cached → не видим edit.
**Fix:** Delete the affected session's cache row:
```bash
sqlite3 data/agent-jobs.db "DELETE FROM compact_store WHERE session_id = '...'"
```

### Persist failures (cache работает, но не растёт)

**Symptom:** `compactor.persist_failed` warnings в logs, `compact_store`
пустая.
**Root cause:** SQLite write contention, disk full, или WAL locked.
**Fix:**
1. Check disk space: `df -h data/`
2. Check `agent-jobs.db` permissions: `ls -la data/agent-jobs.db`
3. Check WAL mode: `sqlite3 data/agent-jobs.db "PRAGMA journal_mode"`
4. Check recent errors в `data/audit/compaction-*.ndjson`

### Cache hit rate низкая (< 5%)

**Symptom:** Большинство compact calls идут в slow path.
**Root cause:** Каждое user message меняет `source_hash` → новая cache row →
никогда не cache hit. Это by design — history редко identical.
**Fix:** Если хотите высокую hit rate, нужно rate-limit compaction
triggering (Phase 4 work).

### Audit log растёт быстро

**Symptom:** `data/audit/compaction-*.ndjson` > 100MB/день.
**Root cause:** `compaction_audit_log=True` на busy server.
**Fix:** Default OFF. Включайте для debug/compliance only.
```bash
COMPACTION_AUDIT_LOG=false  # default
```

## Known Limitations

1. **Per-call override cache** — `compactor.maybe_compact(messages, model,
   session_id=...)` принимает per-call session_id, но `source_hash` не
   per-call — `source_hash` для cache hit comparison всегда вычисляется
   из input messages. Если history меняется каждый call, hit rate низкая.
2. **No retention policy** — `compaction_cache_max_versions` setting
   присутствует, но pruning логика ещё не реализована (Phase 4).
3. **No cross-session continuity** — cache per-session, не per-user.
   Compact summary в L2 mem0 (`#compact` tag) — единственный cross-session
   канал.
4. **Audit log no rotation** — JSONL файл растёт пока вручную не
   удалится. Phase 4 добавит daily archive.
5. **No Prometheus counters** — Phase 4 observability. Phase 3.5 —
   structured logs + JSONL only.

## Out of Scope (Phase 4+)

- **API endpoint** `POST /api/v1/sessions/{id}/compact` (manual operator trigger)
- **Background worker** (cron-style scan for over-threshold sessions)
- **Cross-session handoff** через L2 (continuity across sessions)
- **Real-time redaction UI dashboard**
- **Compaction policy DSL** (per-session settings override)
- **Compaction replay/rollback UI**
- **Pruning implementation** для `compaction_cache_max_versions`
- **Prometheus metrics** для cache hit rate

## Summary Metrics

- **Commits:** 5 (Step 0..4) — `5a6fe6b`, `f9a5d0a`, `5741dbf`, `122857a`, TBD
- **Tests:** 1026 mock + 5 real_llm (от 968 в v1.0.0, +58 new)
- **New files:** 7
  - `harness/agents/compact_store.py` (~200 LoC)
  - `harness/context/compaction_audit.py` (~70 LoC)
  - `tests/test_compact_store.py` (25 tests)
  - `tests/test_compactor_cache.py` (12 tests)
  - `tests/test_phase35_wiring.py` (11 tests)
  - `tests/test_compactor_observability.py` (10 tests)
  - `docs/PHASE3.5.md` (this file)
- **Modified files:** 5
  - `harness/context/compaction.py` (+120 LoC)
  - `harness/server/agent/session.py` (1 line: pass session_id)
  - `harness/server/app.py` (+30 LoC: lifespan wiring)
  - `harness/config.py` (+45 LoC: 3 settings + validator)
  - `tests/test_compactor_cache.py` (1 assertion fix)
- **New LoC:** ~290 production + ~600 tests
- **New deps (required):** 0
- **Tag:** v1.1.0 (annotated)
- **Breaking changes:** None
