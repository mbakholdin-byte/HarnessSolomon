# Phase 3 v1.4.0 — Reflection + Manual /compact + Prompt Caching

**Status:** ЗАКРЫТО v1.4.0, 2026-06-15. Phase 3 = **11/12 closed**.

Production extension поверх Phase 3 v1.3.1 (Tool Offload). Реализует финальные **3 стратегии Anthropic context engineering playbook** (Write / Select / Compress / Isolate). После v1.4.0 остаётся **1 phase** (v1.5.0 — Privacy zones + pre-compaction hook) до полного закрытия Phase 3.

---

## 1. What & why

Anthropic-4 strategies context engineering:

| Strategy | Phase | Status |
|----------|-------|--------|
| **Write context** (scratchpad / plan / notes) | v1.2.0 | ✅ ЗАКРЫТО |
| **Select context** (L2 retriever, hybrid + RRF) | v1.3.0 | ✅ ЗАКРЫТО |
| **Compress context** (sliding window + LLM summary) | v1.0.0 + v1.1.0 + v1.3.5 | ✅ ЗАКРЫТО |
| **Isolate context** (sub-agents, worktrees) | v1.3.1 | ✅ ЗАКРЫТО |
| **Manual /compact** (user-triggered) | **v1.4.0** | ✅ NEW |
| **Background summarisation** (reflection) | **v1.4.0** | ✅ NEW |
| **Prompt caching** (Anthropic cache_control) | **v1.4.0** | ✅ NEW |

`v1.4.0` adds the three remaining context-management primitives. None of them require a new LLM provider or external dependency — the harness stays local-first.

---

## 2. Components

### 2.1 `SessionLifecycle` — end-of-session hook

`harness/server/agent/lifecycle.py` (~155 LoC). Async context manager that fires `ReflectionLoop.reflect()` on `__aexit__`. The hook is invoked from three trigger paths:

* **WS disconnect** — `async with SessionLifecycle(...)` wraps the receive loop in `harness/server/routes/chat.py`.
* **CLI exit** — `harness agents run` uses the same wrapper around the `AgentLoop.run` call.
* **API session close** — same pattern, exposed via `app.state.reflection_factory` for callers that want their own close semantics.

**Fail-open.** Timeout, exception, missing router — all logged + swallowed. Reflection is a side effect, not a gate.

**Per-call timeout.** `asyncio.wait_for(reflect(...), timeout=reflection_max_ms/1000)`. Default 10 s; the long-poll of the WS loop never gets stuck.

**Trust boundary.** Reads `runtime._reflection` via `getattr(self.runtime, "_reflection", None)`. `runner.py` does NOT import `SessionLifecycle` (verified by `test_runner_does_not_import_session_lifecycle`).

### 2.2 `ReflectionLoop` — T1 → T2 cascade with fail-open

`harness/server/agent/reflection_loop.py` (~340 LoC).

#### Public API

```python
@dataclass(frozen=True)
class SessionEvent:
    kind: Literal["user", "assistant", "tool"]
    content: str
    ts: float
    tool_name: str | None = None
    offloaded_id: int | None = None  # v1.3.1 offload integration

@dataclass(frozen=True)
class Lesson:
    kind: Literal["gotcha", "preference", "pattern"]
    content: str
    tags: list[str] = field(default_factory=list)

class ReflectionLoop:
    def __init__(
        self,
        scratchpad: Any | None,
        settings: Any,
        *,
        router: Any | None = None,
        unified_memory: Any | None = None,
        audit: Any | None = None,
    ) -> None: ...

    async def reflect(self, events: list[SessionEvent]) -> list[Lesson]:
        """Returns [] on any failure. Dual-writes lessons on success."""
```

#### Cost cascade

1. Try `settings.reflection_model` (empty → `subagent_t1_model` → `qwen3:8b`).
2. On any error (network, timeout, JSON parse) → fall back to `settings.reflection_fallback_model` (empty → `subagent_t2_model` → `glm-4.7`).
3. If both fail → return `[]` + audit `reflection_cascade_failed`.

#### JSON parse tolerance

The model is asked to return a strict JSON list. We tolerate:
* Code fences (`````json ... `````)
* Leading prose before the first `[`
* Trailing prose after the last `]`
* Extra fields (dropped silently)

On any parse failure → return `[]` + audit `reflection_parse_failed` with the first 200 chars of the raw response.

#### Dual-write

Each extracted lesson is written to:
1. **Scratchpad L1** with tags `#reflection`, `#session/{id}`, and the lesson's own tags. The L1 layer is the per-session journal; the next session can pull from it via the L1 → L0 promotion flow.
2. **UnifiedMemory L1** with `source="reflection"`. This is the cross-session store.

If either write fails, the other still runs. Failure is logged + swallowed.

#### Cap

`settings.reflection_max_lessons` (default 5). Even if the model returns 50 lessons, we keep the first N. Bounds the cost of the dual-write step.

### 2.3 `CompactTrigger` — manual /compact wrapper

`harness/server/agent/compact_trigger.py` (~140 LoC). Thin wrapper around `ContextCompactor.force_compact()` (added in Step 0) with explicit per-call timeout and audit.

#### Public API

```python
class CompactTrigger:
    def __init__(
        self,
        compactor: Any | None,
        settings: Any,
        *,
        audit: Any | None = None,
    ) -> None: ...

    async def compact_now(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        session_id: str,
        bypass_cache: bool = False,
    ) -> CompactResult | None:
        """Returns the result on success, None on any failure."""
```

#### Three trigger paths

| Path | Code | Auth |
|------|------|------|
| **HTTP** | `POST /api/v1/sessions/{id}/compact?bypass_cache=false` | `sessions.write` |
| **CLI** | `harness sessions compact --session <id> [--bypass-cache]` | n/a (HTTP client) |
| **WS** | `{"type": "compact", "bypass_cache": false}` (response: `{"type": "compact_done", ...}`) | n/a (in-WS) |

#### Failure semantics

* `compactor is None` → `None` + audit `compact_unavailable`
* `force_compact` raises → `None` + audit `compact_failed` (error in payload)
* `asyncio.wait_for` fires → `None` + audit `compact_timeout` (max_ms in payload)
* HTTP route returns 503 in all failure cases
* WS handler sends `{"type": "compact_failed", "error": "..."}`

### 2.4 `LLMRouter._maybe_inject_cache_control` — Anthropic cache_control

`harness/server/llm/router.py` (+~95 LoC). Router-level injection in both `completion()` and `streaming_completion()`.

#### Logic

```python
if (
    settings.prompt_cache_enabled
    and settings.prompt_cache_strategy == "anthropic"
    and model_id.startswith("anthropic/")
):
    cache_control = {"type": "ephemeral"}
    for i, msg in enumerate(messages):
        if i == 0 or i == len(messages) - 1 or i == len(messages) - 2:
            new_msg["cache_control"] = cache_control
```

#### What it does

* Marks the **system message** (index 0) — Anthropic caches the largest stable prefix
* Marks the **last 2 messages** — typical "fresh context" insertion point
* Does **not** mutate the input — returns a copy with markers added
* Preserves all existing fields (`name`, `metadata`, `tool_calls`, ...)

#### What it does NOT do

* For `prompt_cache_strategy="vllm"` — vLLM prefix caching is an engine-level feature, the operator configures vLLM externally. Harness has no work to do.
* For `prompt_cache_strategy="off"` — no markers added.
* For non-Anthropic models — no markers added (the model wouldn't understand them).

#### Why router-level (not provider-level)?

The plan agent review (Phase 3 v1.4.0) flagged that adding a dedicated Anthropic provider module is out of scope for the 12-week roadmap. The router is the only place that already knows about the model id, and it forwards the message list as-is to litellm. We mutate a *copy* of the list / message dicts so callers are not surprised by side effects.

---

## 3. Settings reference

All 8 new settings are in `harness/config.py`, all default ON, all configurable, all validated at startup.

| Setting | Default | Validator | Group |
|---------|---------|-----------|-------|
| `reflection_enabled` | `True` | `bool` | Reflection |
| `reflection_max_lessons` | `5` | `ge=1, le=20` | Reflection |
| `reflection_max_ms` | `10000` | `ge=100, le=60000` | Reflection |
| `reflection_model` | `""` (→ `subagent_t1_model`) | `str` | Reflection |
| `reflection_fallback_model` | `""` (→ `subagent_t2_model`) | `str` | Reflection |
| `manual_compact_max_ms` | `30000` | `ge=1000, le=120000` | Manual /compact |
| `prompt_cache_enabled` | `True` | `bool` | Prompt caching |
| `prompt_cache_strategy` | `"off"` | `Literal["anthropic", "vllm", "off"]` | Prompt caching |

**To enable Anthropic prompt caching**, set:
```bash
PROMPT_CACHE_ENABLED=true
PROMPT_CACHE_STRATEGY=anthropic
```

**To enable reflection lessons** (it's on by default):
```bash
REFLECTION_ENABLED=true
REFLECTION_MAX_LESSONS=5
```

**To disable manual /compact timeout** (use the 30 s default):
```bash
MANUAL_COMPACT_MAX_MS=30000
```

---

## 4. `SESSIONS_WRITE` scope

`harness/server/auth/scopes.py` — new enum value `Scope.SESSIONS_WRITE = "sessions.write"`.

**Why a new scope?** `/compact` is a session-control operation (it compacts the running session's context), not a job-write and not a memory-write. Reusing `memory.write` would conflate two semantically different operations. The new scope lets operators grant `/compact` separately from `agents.write` (create new jobs) and `memory.write` (write to L1/L2).

**Auto-registration** in `ALL_SCOPES = frozenset(Scope)` and `SCOPE_DESCRIPTIONS`:
```
"Force-compact a session's context (POST /api/v1/sessions/{id}/compact, Phase 3 v1.4.0)"
```

**Wire in tests** — `test_capabilities.py::test_all_seven_scopes_listed` updated (was `test_all_six_scopes_listed`).

---

## 5. SessionLifecycle model

The lifecycle is a thin async context manager that owns ONE thing: the end-of-session hook.

```
[client]  →  [WS connect]  →  [events_collector: list]  →  [agent loop runs]
                                       ↓
                              (assistant + tool events appended)
                                       ↓
[client]  →  [WS disconnect]  →  [lifecycle.__aexit__]  →  [reflection.reflect()]
                                                                        ↓
                                                            [T1 cascade → lessons]
                                                                        ↓
                                              [dual-write: scratchpad L1 + UnifiedMemory L1]
                                                                        ↓
                                                            [audit reflection_extracted]
```

**Three key properties:**

1. **Stateless enter / stateful exit.** `__aenter__` returns self. The work happens on exit.
2. **Failure isolation.** A bad reflection call never breaks the user-facing response — the WS close, CLI exit, or API response goes out on time.
3. **Per-call timeout.** The session never stalls because the LLM is hung. We have a hard deadline.

---

## 6. Migration guide

**v1.3.x → v1.4.0**

1. **Default behaviour changes** — `prompt_cache_enabled=True` and `reflection_enabled=True` mean the harness now does extra work. To opt out, set the relevant settings to `False`.
2. **New scope required** for `POST /api/v1/sessions/{id}/compact` — the test plan needs to mint a `SESSIONS_WRITE` token.
3. **WS protocol** — clients should now expect `{"type": "compact_done", ...}` and `{"type": "compact_failed", ...}` events if they send `{"type": "compact", ...}`.
4. **No new dependencies** — `pip install -e .` works the same.
5. **No DB migrations** — same SQLite files, same schemas.

**Upgrade command** (no-op, but verifies):
```bash
git pull
pip install -e .
harness serve --port 8765
# verify: GET /api/v1/capabilities shows 7 scopes (was 6)
```

---

## 7. Trust boundary

`runner.py` does NOT import any of:

* `ReflectionLoop` (Step 2)
* `SessionLifecycle` (Step 1)
* `CompactTrigger` (Step 3)
* `force_compact` (Step 0 — added to `compaction.py`, already trusted)
* `cache_control` (Step 4 — only `router.py` injects, trusted zone)

**Static tests** (in `tests/test_agent_runner.py`):
* `test_runner_does_not_import_reflection_loop`
* `test_runner_does_not_import_session_lifecycle`
* `test_runner_does_not_import_compact_trigger`

Mirrors v1.3.1 `test_runner_does_not_import_tool_offloader` pattern. Each test greps `runner.py` for the module path / class name and fails on any direct reference.

---

## 8. Reuse from earlier phases

| Pattern | Source | Re-use in v1.4.0 |
|---------|--------|-----------------|
| Factory pattern (mirrors `offloader_factory`) | `runner.py:231-247` | `reflection_factory` в `runner.py` |
| Trust boundary: `runner.py` does NOT import module | `test_runner_does_not_import_*` | 3 new static tests |
| `getattr` chain для session_id в AgentLoop | `_maybe_offload_tool_result` (`loop.py:418-435`) | `_record_event` |
| `asyncio.wait_for` для per-call timeout | `tool_offload_max_ms/1000` | `reflection_max_ms`, `manual_compact_max_ms` |
| Fail-open pattern (try/except + logger.warning) | `ToolOffloader.offload` | `ReflectionLoop.reflect`, `CompactTrigger.compact_now`, `LLMRouter._maybe_inject_cache_control` |
| Audit integration через `ScratchpadAudit.record(event="...")` | `tool_offload` event | `reflection_extracted`, `reflection_parse_failed`, `reflection_cascade_failed`, `manual_compact`, `compact_failed`, `compact_timeout`, `compact_unavailable` |
| `_run_slow_path` extraction refactor | Phase 3 v1.4.0 Step 0 | `force_compact` (compact_now → _run_slow_path) |

---

## 9. Out of scope (Phase 3 v1.5.0+)

* **Privacy zones** — restrict which tools / data reflection can read. v1.5.0.
* **Pre-compaction hook** — let user code run before `force_compact` / `maybe_compact` (e.g. to save custom state). v1.5.0.
* **Time-based / token-based auto-compaction triggers** — current `maybe_compact` is auto (token-based), `force_compact` is manual. v1.5.0 will add time-based and event-based.
* **Anthropic + vLLM provider subclasses** — v1.4.0 uses litellm-router-level injection, not dedicated providers.
* **Stacked reflection** — multiple sessions reflecting together. v1.5.0+.
* **Reflection search via L2Retriever** — Phase 3 v1.3.0 retriever reuse not needed for v1.4.0 — reflection uses `subagent_t1_model` directly, not L2 search.

---

## 10. Next

Phase 3 v1.5.0 — **Privacy zones + Pre-compaction hook** (1 remaining, 12/12).

Phase 4 — **12 hooks + observability (Prometheus) + /api/* → /api/v1/* migration**.

Phase 5 — **Eval harness + cascade calibration**.
