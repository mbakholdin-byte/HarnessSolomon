# Phase 3 v1.5.0 — Privacy zones + Pre-compaction hook + Time-based trigger

> **Status:** ЗАКРЫТО v1.5.0 (2026-06-15) — **Phase 3 = 12/12 closed (FINAL)**
> **Tag:** `v1.5.0` (annotated)
> **Tests:** ~1434 mock (от 1281 в v1.4.0, +150 net, +2 skip)
> **Anthropic playbook:** "Isolate sensitive context" + "PreCompact hook" + "Manual compact" extended (turn/time triggers)

## TL;DR

Phase 3 v1.5.0 закрывает **последние 3 фичи** в roadmap Phase 3 (11/12 → 12/12) и **полностью завершает Phase 3** (Context Engineering по Anthropic playbook). 5 production модулей + 3 new test classes + 1 trust boundary test + 11 new settings.

**Три проблемы, которые решает v1.5.0:**

1. **Privacy leak** — `private/*`, `.env`, `secrets/*`, `_credentials/*`, `**/.ssh/*` могут прочитаться `read_file`/`grep`/`glob` и утечь в auto-memory через scratchpad write → LLM. **PrivacyZoneFilter** блокирует/reдактирует на Tier 1 sinks (read tools).
2. **Pre-compact state loss** — manual `/compact` (v1.4.0) теряет high-signal state (last messages, plan, hot L0) до того, как reflection извлёк. **PreCompactHook** fires в `_run_slow_path` BEFORE sliding window, сохраняет state в UnifiedMemory L1 с тегом `#pre-compact-{session_id}`.
3. **Compaction triggers = token-only** — длинные low-token сессии или idle sessions никогда не compact'ятся. **TimeBasedCompactionTrigger** добавляет 3 новых режима: turn (каждые N user turns), time (после M idle минут), hybrid (OR).

## Архитектура (одобрено Марком + Plan agent review)

| Решение | Значение |
|---------|----------|
| **Privacy glob matching** | `match_glob(path, pattern) -> bool` — single source of truth в `harness/privacy/path_match.py` (извлечён из `pr_templating.py:262-299`). **Plan agent BLOCKER B1** — без извлечения pattern semantics дрейфуют. |
| **PrivacyZoneConfig parsing** | `privacy_zone_patterns: str = ""` comma-separated, парсится в `model_validator` (mirror `redaction_categories` pattern). Empty = use 7 built-in defaults. |
| **Per-zone action override** | `privacy_zone_per_action: str = "private/**=redact,secrets/*=block"`. Default = `privacy_zone_default_action: Literal["block", "redact", "skip"]`. |
| **PrivacyZoneFilter integration (3 tier'а)** | **Tier 1 (MUST, v1.5.0)**: `read_file` / `grep` / `glob` (ToolRuntime, 3 lines per tool); **Tier 2 (DEFERRED to v1.6.0+)**: `scratchpad.write_note`, `_persist_summary` metadata tag; **Tier 3 (DEFERRED)**: embedder path metadata, WebSocketChat, OutboundWebhookDispatcher. |
| **PreCompactHook location** | ВНУТРИ `_run_slow_path` (Plan agent BLOCKER B4), ПОСЛЕ cache-miss-check, ПЕРЕД `_sliding_window`. На cache hit — НЕ fired (state уже сохранён при прошлом compact). |
| **PreCompactHook timeout** | `asyncio.wait_for(hook(...), timeout=pre_compact_max_ms/1000)` (mirror `CompactTrigger.compact_now()` v1.4.0). Fail-open: timeout/exception → log + audit + return. |
| **PreCompactHook state** | `PreCompactState` (frozen dataclass): `session_id`, `messages_last_n` (5), `plan_step` (from scratchpad L1), `hot_l0` (from scratchpad L0), `metadata`, `captured_at`. Configurable `pre_compact_save_fields`. |
| **TimeBasedCompactionTrigger state** | `last_compact_at: dict[session_id, float]` + `last_user_turn: dict[session_id, int]` per session. `asyncio.Lock` per session_id (Plan agent BLOCKER B3). |
| **Trigger modes** | `compaction_trigger: Literal["token", "turn", "time", "hybrid"]` — default `"token"` (backward compat). `"hybrid"` = OR semantics. |
| **Resume vs active distinction** | `force_idle_check=False` default (opt-in). `Session.load_history` → `False` (Plan agent BLOCKER B8). `AgentLoop.run` → `True` explicitly. |
| **Trust boundary** | `runner.py` does NOT import: `PrivacyZoneFilter`, `PreCompactHook`, `TimeBasedCompactionTrigger`. 1 parametrized test `test_runner_does_not_import_v150_module` (3 cases). |
| **Scopes** | БЕЗ new scope. Per-session override = admin env var `HARNESS_PRIVACY_ZONES_DISABLED=true`. |
| **Audit events** | `privacy_zone_blocked`, `privacy_zone_redacted`, `privacy_zone_skipped`, `pre_compact_state_saved`, `pre_compact_failed`, `pre_compact_timeout`. |
| **Fail-open** | Privacy zone hit (read_file) → `ToolResult(ok=False, error="path in privacy zone: ...")` (NOT silent — LLM must know action blocked). Pre-compact hook timeout → log + audit + return None. Time trigger eval error → skip. |

## 11 new settings (45 → 56)

### Privacy zones (5)
```python
privacy_zones_enabled: bool = True
privacy_zone_patterns: str = ""  # Comma-separated. Empty = 7 built-in defaults.
privacy_zone_default_action: Literal["block", "redact", "skip"] = "block"
privacy_zone_per_action: str = ""  # "private/**=redact,secrets/*=block"
privacy_zones_audit_log: bool = False  # → ScratchpadAudit mirror
```

### Pre-compact hook (3)
```python
pre_compact_enabled: bool = True
pre_compact_max_ms: int = 5000  # per-call timeout
pre_compact_save_fields: str = "messages_last_n,plan_step,hot_l0,metadata"  # Comma-separated
```

### Time-based trigger (3)
```python
compaction_trigger: Literal["token", "turn", "time", "hybrid"] = "token"  # backward compat
compaction_turn_interval: int = 20  # user turns between compacts
compaction_time_idle_minutes: int = 30  # minutes of inactivity
```

## Default privacy patterns (7 built-in)

```
private/**       # private dirs anywhere
*.env            # dotenv files at any level
.env/*           # dotenv directory contents
secrets/*        # secrets directory
_credentials/*   # credentials directory
**/.ssh/*        # SSH directory contents (any depth)
```

Override via `privacy_zone_patterns: "internal/**,prod/*,*.key"`.

## Privacy flow (Tier 1 sinks)

```
read_file(path)        → PrivacyZoneFilter.check(path) → block/redact/skip/allow
grep(pattern, path)    → same check (path-level)
glob(pattern)          → same check per matched file
```

**Block** → `ToolResult(ok=False, error="path in privacy zone: X (matched: Y)")`. LLM видит ошибку и адаптирует стратегию.
**Redact** → `ToolResult(ok=True, output="[PRIVATE: matched Y]")`. LLM знает, что путь приватный.
**Skip** → `ToolResult(ok=True, output="")`. Path silently excluded (для grep/glob с многими путями).

## Pre-compact flow

```
force_compact(messages, session_id)
  → ContextCompactor._run_slow_path
    → [NEW v1.5.0] PreCompactHook.capture(session_id, messages, metadata)
      → if save_fields contains "messages_last_n" → last 5 user/assistant
      → if save_fields contains "plan_step" → from scratchpad L1 tag="plan"
      → if save_fields contains "hot_l0" → from scratchpad L0 notes
      → if save_fields contains "metadata" → pass-through
      → UnifiedMemory.write(text, tags=["#pre-compact-{session_id}", "#session/{session_id}"])
      → fail-open at: capture / read_notes / write / audit (4 fail-open layers)
    → _sliding_window(messages, target)
    → if trimmed ≤ target: return trimmed
    → _summarise(dropped) → summary
    → _inject_summary(trimmed, summary) → compacted
    → _persist_summary(summary) → UnifiedMemory (L2, #compact tag)
```

## Time-based trigger flow

```
maybe_compact(messages, session_id, force_idle_check=False)
  → if idle_trigger is not None and force_idle_check:
    → if idle_trigger.should_trigger(session_id, messages):
      → ctx = _model_ctx(model, settings)
      → target = int(ctx * settings.compaction_target_ratio)
      → tokens = _estimate_tokens(messages)
      → compacted = await _run_slow_path(messages, session_id, target, tokens, cache_enabled=False)
      → idle_trigger.mark_compacted(session_id, messages=compacted)
      → return compacted
  → [existing token-threshold path]
  → tokens = _estimate_tokens(messages)
  → if tokens ≤ threshold: return messages
  → [cache lookup if enabled]
  → _run_slow_path(...)
  → if idle_trigger is not None: mark_compacted (token-mode bookkeeping)
  → return compacted
```

**Mode logic** (TimeBasedCompactionTrigger):
- `token` (default): trigger never fires — legacy behaviour, compactor uses token threshold.
- `turn`: `user_turns_now - last_user_turn[session_id] >= turn_interval` (default 20).
- `time`: `now - last_compact_at[session_id] >= idle_minutes * 60` (default 30 min).
- `hybrid`: OR of turn + time (token check is separate in compactor).

First call for a session **seeds** baseline (does NOT fire) — avoids false-positive on the very first turn / time check.

## Trust boundary (preserved через все 5 шагов)

- `runner.py` does NOT import: `PrivacyZoneFilter`, `PreCompactHook`, `TimeBasedCompactionTrigger`
- 1 parametrized test `test_runner_does_not_import_v150_module` (3 cases) — mirror v1.4.0 pattern
- All new modules DI'd через constructors (factory callable pattern, mirror `scratchpad_factory` / `offloader_factory` / `reflection_factory`)
- `privacy_zones=None` default в `ToolRuntime` — backward compat
- `pre_compact_hook=None` default в `ContextCompactor` — backward compat (existing 5+ tests ctor with default still pass)
- `idle_trigger=None` default в `ContextCompactor` — backward compat
- Fail-open pattern: privacy hit → `ToolResult(ok=False, ...)` (NOT silent); pre-compact timeout → log + audit + return None; time trigger error → skip
- Per-call timeout via `asyncio.wait_for(..., timeout=*_max_ms/1000)` — keeps LLM loop responsive

## End-to-end verification (после всех 5 шагов)

```bash
cd C:/MyAI/06_Harness
python -m pytest tests/ -q                           # expect 1431 tests pass (+ 2 skip)
python -c "from harness.privacy.zone_filter import PrivacyZoneFilter; print('ok')"
python -c "from harness.agents.pre_compact import PreCompactHook; print('ok')"
python -c "from harness.agents.idle_trigger import TimeBasedCompactionTrigger; print('ok')"
python -c "from harness.config import settings; print(settings.privacy_zones_enabled, settings.compaction_trigger)"
git tag --list                                      # shows v1.4.0 v1.5.0
```

## Migration guide (zero breaking changes)

**For existing users** (v1.4.0 → v1.5.0):
- Default mode `compaction_trigger = "token"` — legacy token-threshold behaviour preserved
- Default `privacy_zones_enabled = True` — но если `privacy_zone_patterns = ""`, используются **7 safe defaults** (private/.env/secrets/ssh)
- Default `pre_compact_enabled = True` — hook fires on every slow-path run; state saved to UnifiedMemory
- **No new required dependencies**
- **No new scopes** (admin override via env var `HARNESS_PRIVACY_ZONES_DISABLED=true`)
- **No CLI changes**

**To opt-out of v1.5.0 features** (one by one):
```bash
# Disable privacy zones entirely
export HARNESS_PRIVACY_ZONES_ENABLED=false
# Or in .env:
# privacy_zones_enabled=false

# Disable pre-compact hook
export HARNESS_PRE_COMPACT_ENABLED=false

# Use legacy token-only compaction trigger
export HARNESS_COMPACTION_TRIGGER=token
```

## Critical files

- NEW: `harness/privacy/{__init__,path_match,zone_config,zone_filter}.py` (~330 LoC)
- NEW: `harness/agents/pre_compact.py` (~280 LoC)
- NEW: `harness/agents/idle_trigger.py` (~250 LoC)
- MODIFIED: `harness/context/compaction.py` (+~150 LoC, ctor + idle_trigger + force_idle_check + _safe_pre_compact_hook + mark_compacted)
- MODIFIED: `harness/server/agent/runtime.py` (+~80 LoC, privacy_zones kwarg + 3 sink calls)
- MODIFIED: `harness/server/agent/loop.py` (+~10 LoC, force_idle_check=True)
- MODIFIED: `harness/server/agent/session.py` (+~5 LoC, force_idle_check=False)
- MODIFIED: `harness/agents/pr_templating.py` (~5 LoC, use shared path_match)
- MODIFIED: `harness/server/app.py` (+~60 LoC, lifespan wiring PrivacyZoneFilter + PreCompactHook + TimeBasedCompactionTrigger)
- MODIFIED: `harness/config.py` (+~80 LoC, 11 new settings in 1 new section)
- TESTS: 7 new test files (~1,920 LoC, 110+ tests):
  - `tests/test_privacy_path_match.py` (15)
  - `tests/test_privacy_zone_config.py` (12)
  - `tests/test_privacy_zones.py` (18)
  - `tests/test_privacy_zones_sinks.py` (14 + 1 skip)
  - `tests/test_pre_compact_hook.py` (21)
  - `tests/test_idle_trigger.py` (22)
  - `tests/test_compactor_v150_integration.py` (7)
  - `tests/test_runner_does_not_import_v150.py` (1 parametrized = 3 cases)
- DOCS: `docs/PHASE3-privacy-precompact-time.md` (NEW, this file)
- CHANGELOG: `docs/CHANGELOG.md` (v1.5.0 section)

## Reused patterns (со ссылками)

- `match_glob` extracted from `_match_codeowners_pattern` at `harness/agents/pr_templating.py:262-299` (single source of truth)
- `ScratchpadStore.write_note(level="L1")` — `harness/agents/scratchpad_store.py:179-252` (pre-compact state save)
- `Settings` Pydantic v2 with `Literal["a", "b", "c"]` validators — `harness/config.py` (privacy_zone_default_action, compaction_trigger)
- `redaction_categories: str` comma-separated pattern — `harness/config.py:933-943` (privacy_zone_patterns mirror)
- Composition strategy (Runner pre-builds + AgentLoop applies) — `harness/agents/runner.py:_format_l0_section` (privacy_zones factory closure)
- Factory callable pattern for trust boundary — `harness/agents/runner.py:231-247` (`scratchpad_factory` / `offloader_factory` / `reflection_factory`)
- `asyncio.wait_for(..., timeout=...)` for per-call timeout — stdlib pattern, mirrors `harness/agents/pr_integration.py:wait_for_checks`, `harness/server/agent/compact_trigger.py:compact_now`
- Fail-open pattern with logger.warning + audit — `harness/server/agent/reflection_loop.py:reflect` (v1.4.0)
- `ScratchpadAudit.record(...)` accepts arbitrary event names — `harness/context/scratchpad_audit.py:43-58`
- `dict[session_id, state]` + `asyncio.Lock` per key — stdlib pattern, mirrors `harness/agents/repo_locks.py:RepoLockRegistry` (Phase 2.2)
- `_run_slow_path` shared between `maybe_compact` and `force_compact` — `harness/context/compaction.py:407` (v1.4.0)
- `__init__ + Any = None` kwarg for trust boundary — pattern from `runner.py:230-289` (PrivacyZoneFilter, PreCompactHook, TimeBasedCompactionTrigger all use this)

## Out of scope (v1.6.0+ / Phase 4+ / Phase 5)

- Hierarchical summarization (L0/L1/L2/L3) → v1.6.0+
- LLMLingua integration → v1.6.0+
- Embedding swap-in (BGE-M3, FRIDA) → Phase 5
- Cross-encoder rerank (bge-reranker) → Phase 5
- 12 hooks (PreToolUse/PostToolUse/Stop/etc.) → Phase 4
- Prometheus metrics (counters/gauges/histograms) → Phase 4
- /api/* → /api/v1/* migration with deprecation headers → Phase 4
- Tier 3 privacy sinks (embedder path metadata, WebSocketChat broadcasts, OutboundWebhookDispatcher payloads) → v1.6.0+ privacy track
- Per-session `privacy_zones_disabled` runtime override → v1.6.0+ (admin env var `HARNESS_PRIVACY_ZONES_DISABLED=true` в v1.5.0)
- `harness privacy zones add/remove` CLI → out of scope (settings-only в v1.5.0)
- `bash` tool command-pattern filtering (block `cat .env`) → out of scope, separate concern
- 12+ hooks pre-compact integration → v1.6.0+ (Phase 4 hooks)
- Eval harness + cascade calibration → Phase 5
