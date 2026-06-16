# Solomon Harness — Phase 4.0 Hooks Plan

**Version:** v1.6.0 (Phase 4.0)
**Author:** Plan-Research sub-agent (Соломон)
**Date:** 2026-06-16
**Status:** DRAFT — pending Mark approval

---

## § 0. Контекст и решения Марка (2026-06-16)

### Что делаем
Phase 4.0 реализует систему hooks для Solomon Harness: 12 hook-событий Claude Code + 3 кастомных (OnMemoryWrite, OnRoutingDecision, OnCompaction). Платформа исполнения хуков: subprocess-стиль (JSON via stdin, exit 0/2) + async-in-process. Built-in хуки: log, validate, block_dangerous, inject_context, autosave. Trust boundary: `harness/hooks/` НЕ импортирует `harness.agents` / `harness.server` (mirror of `harness/eval/` boundary).

### Scope (Phase 4.0)
- Hook framework: `registry.py` + `runner.py` + `context.py` + `http.py` + `llm_hook.py`
- 15 hook events (12 CC + 3 custom), all with JSON payload schemas
- 5 builtin hooks in `harness/hooks/builtin/`
- Integration points в существующем коде: 9 trigger points (см. § 3)
- Backward compat с `PreCompactHook` (Phase 3 v1.5.0)
- Settings: 15+ new fields в `harness/config.py`
- Static test `test_hooks_trust_boundary.py` (mirror of `test_eval_trust_boundary.py`)
- Tag: v1.6.0

### Явные не-цели (Phase 4.0)
- **Observability** (structured JSONL logs, OpenTelemetry, Prometheus, health checks) — Phase 4.1+
- **Hot-reload** хуков через file watcher — Phase 4.2+
- **Hot-reload `.harness/agents/*.md`** — Phase 4.2+ (carryover)
- **/api/* → /api/v1/* migration** — Phase 4.3+
- **Elicitation / Notification / PermissionRequest events** — Phase 4.4+ (3 CC events из 12 отложены в Phase 4.0: PermissionRequest интегрируется как pre-tool hook на check_deny; Elicitation + Notification — нет внутренних trigger points, deferred)
- **Async webhooks (outbound hooks)** — Phase 4.0 = только INBOUND trigger points + HTTP transport for INBOUND, no outbound (существующий `harness.agents.outbound` — другая подсистема)
- **CLI `harness hooks ...`** — Phase 4.5+ (Phase 4.0 = programmatic API + Settings, no CLI yet)

### Архитектурные решения Марка
- **Trust boundary strict:** `harness/hooks/` не импортирует `harness.agents` или `harness.server`. Точка. Static test enforces.
- **0 new deps.** Stdlib (`asyncio`, `json`, `subprocess`, `dataclasses`, `enum`, `pathlib`, `hashlib`, `hmac`, `time`, `logging`, `typing`) + existing (pydantic, pydantic-settings, aiosqlite). HTTP клиент — `urllib.request` (stdlib) instead of httpx (which is dependency). LLM-as-hook: route through existing `LLMRouter` via DI (no new transport).
- **Plan review обязателен.** 5+ BLOCKERS, 5+ RISKS, 5+ CONCERNS зафиксированы в § 12 до coding.
- **Backward compat с PreCompactHook:** Phase 4.0 hooks framework обратно совместимо интегрирует существующий `pre_compact_hook=callable` в `ContextCompactor.__init__` через auto-registration: при construction compactor, если `pre_compact_hook` is set, runner-уровень `HookRegistry` auto-registers a builtin `OnPreCompact` wrapper.

---

## § 1. Цели и не-цели

### Цели Phase 4.0
1. **12+3 hook events** интегрированы в 9 trigger points (см. § 3).
2. **Runner framework:** синхронные + асинхронные + HTTP + LLM хуки работают.
3. **5 builtin хуков** с default поведением (см. § 5).
4. **Settings-driven:** все 15+ new settings с default + validator (§ 6).
5. **Trust boundary enforced:** `harness/hooks/` import-isolation verified by static test.
6. **Backward compat:** `PreCompactHook` продолжает работать без изменений кода в compactor.py.
7. **Tag v1.6.0** на master с зелёным mock suite (>= 1500 tests).

### Не-цели (Phase 4.0)
- Полный observability stack (deferred to Phase 4.1)
- Hot-reload (deferred to Phase 4.2)
- `/api/* → /api/v1/*` migration (deferred to Phase 4.3)
- Elicitation + Notification events (deferred to Phase 4.4)
- `harness hooks` CLI subcommand (deferred to Phase 4.5)
- Cross-hook data flow (Phase 4.0 = fire-and-forget, Phase 4.6 = state propagation)

---

## § 2. Архитектура

### § 2.1. Модульная структура

```
harness/hooks/                            # NEW: trust-boundary isolated
├── __init__.py                            # Public API exports
├── events.py                              # EventType enum + payload schemas
├── context.py                             # HookContext dataclass + HookDecision
├── registry.py                            # HookRegistry (event → [hooks])
├── runner.py                              # HookRunner (dispatch + timeout + JSON)
├── http.py                                # HttpHook transport (urllib-based)
├── llm_hook.py                            # LLMHook (LLM-as-hook via LLMRouter)
├── filter_chain.py                        # Allow/deny/match-hooks for events
├── audit.py                               # HookAuditSink (NDJSON mirror)
└── builtin/
    ├── __init__.py                        # Auto-register all 5 builtins
    ├── log.py                             # log_*: emit to logger
    ├── validate.py                        # PreToolUse: Pydantic schema check
    ├── block_dangerous.py                 # PreToolUse: regex denylist (defence in depth)
    ├── inject_context.py                  # UserPromptSubmit: prepend L0/L1 snapshot
    └── autosave.py                        # SessionEnd: persist session to L4
```

### § 2.2. Диаграмма (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       TRIGGER POINTS (production code)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  ToolRuntime.execute()   ─── fires ─── PreToolUse / PostToolUse              │
│  AgentLoop.run()         ─── fires ─── Stop (max_iter)                       │
│  AgentRunner.run()       ─── fires ─── SubagentStart / SubagentStop          │
│  server.app.lifespan()   ─── fires ─── SessionStart / SessionEnd             │
│  chat.py WS handler      ─── fires ─── UserPromptSubmit                      │
│  ContextCompactor.maybe_compact() ─── fires ─── PreCompact / OnCompaction   │
│  AgentSpec.parse_agent_md()      ─── fires ─── InstructionsLoaded           │
│  UnifiedMemory.write()   ─── fires ─── OnMemoryWrite                         │
│  LLMRouterClassifier.classify()  ─── fires ─── OnRoutingDecision             │
│  ToolRuntime._bash()     ─── fires ─── PermissionRequest (deny override)    │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          HOOK FRAMEWORK (harness/hooks/)                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  EventType   │  │ HookRegistry │  │  HookRunner  │  │ HookContext  │    │
│  │   (enum)     │──│  (in-mem)    │──│  (dispatch)  │──│  (dataclass) │    │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘    │
│         │                │                  │                                 │
│         │  filter_chain  │  async          │  timeout per hook              │
│         │   (match_glob) │  dispatch       │  (settings.hooks_default_max_ms)│
│         │                │                 │                                 │
│         ▼                ▼                 ▼                                 │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │  Hook transport: in-process | subprocess | http | llm          │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│         │                │                 │                                 │
│         ▼                ▼                 ▼                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  builtin/    │  │  user .py    │  │  HTTP        │  │  LLM-as-hook │    │
│  │  (5 hooks)   │  │  (subprocess)│  │  (urllib)    │  │  (router DI) │    │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘    │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │  HookAuditSink: NDJSON mirror (opt-in via hooks_audit_log=True)  │       │
│  └──────────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       BUILTIN HOOKS (5)                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│  builtin/log.py           — log_<event> via stdlib logging                   │
│  builtin/validate.py      — PreToolUse: Pydantic schema check               │
│  builtin/block_dangerous.py — PreToolUse: regex denylist (defence in depth)  │
│  builtin/inject_context.py — UserPromptSubmit: prepend L0/L1 snapshot       │
│  builtin/autosave.py      — SessionEnd: persist session to L4               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### § 2.3. Core Types

#### `harness/hooks/events.py`
```python
from __future__ import annotations
import enum

class EventType(str, enum.Enum):
    """All 12 CC hook events + 3 custom."""
    # === 12 CC events ===
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_COMPACT = "PreCompact"
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    PERMISSION_REQUEST = "PermissionRequest"
    # Elicitation + Notification — DEFERRED to Phase 4.4

    # === 3 custom ===
    ON_MEMORY_WRITE = "OnMemoryWrite"
    ON_ROUTING_DECISION = "OnRoutingDecision"
    ON_COMPACTION = "OnCompaction"
```

#### `harness/hooks/context.py`
```python
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Literal

@dataclass(frozen=True)
class HookContext:
    """Payload for a single hook invocation.

    All 15 events share this base shape; event-specific fields
    are added via subclassing (TypedDict-style).
    """
    event: str                          # EventType.value
    session_id: str                      # Current session UUID
    agent_id: str                        # Current agent id ("" for main session)
    payload: dict[str, Any]              # Event-specific payload
    ts: float = field(default_factory=time.time)
    # --- request-scoped state ---
    request_id: str = ""                 # Optional: matches LLM call id

@dataclass(frozen=True)
class HookDecision:
    """Result of a single hook execution.

    Semantics:
      - ``continue=True`` + ``decision="allow"`` → proceed
      - ``continue=True`` + ``decision="block"`` → caller raises (PreToolUse)
      - ``continue=False`` + ``decision="error"`` → exception in hook
    """
    hook_id: str                         # Registered hook id
    event: str                           # EventType.value
    decision: Literal["allow", "block", "modify", "error"] = "allow"
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
```

#### `harness/hooks/registry.py`
```python
class HookRegistry:
    """Event → [HookSpec] mapping.

    Thread-safe via GIL (asyncio is single-threaded). In-memory
    only for Phase 4.0; Phase 4.2 will add file-watcher reload.
    """
    def register(self, event: EventType, hook: HookSpec) -> None: ...
    def unregister(self, event: EventType, hook_id: str) -> None: ...
    def get(self, event: EventType) -> list[HookSpec]: ...
    def clear(self) -> None: ...

@dataclass(frozen=True)
class HookSpec:
    """Single registered hook."""
    hook_id: str                                # Unique within registry
    event: EventType
    transport: Literal["builtin", "subprocess", "http", "llm"]
    # builtin: ``handler: Callable[[HookContext], Awaitable[HookDecision]]``
    # subprocess: ``command: str`` + ``timeout_ms: int``
    # http: ``url: str`` + ``method: Literal["POST"]`` + ``headers: dict``
    # llm: ``prompt: str`` + ``model: str`` + ``router: LLMRouter``
    handler: Callable[..., Any] | None = None
    command: str = ""
    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    model: str = ""
    # Filter chain (match_glob-style from privacy/zone_filter.py)
    matcher: Callable[[HookContext], bool] | None = None
    timeout_ms: int = 5000
    priority: int = 100
    enabled: bool = True
```

#### `harness/hooks/runner.py`
```python
class HookRunner:
    """Dispatch hook events to registered HookSpecs.

    Decision aggregation: when multiple hooks fire for the same event,
    the runner applies them in priority order (lower = first). The
    first ``decision="block"`` short-circuits and returns that decision
    to the caller. ``decision="modify"`` returns a ``modified_payload``
    that the caller should use instead of the original.
    """
    def __init__(self, registry: HookRegistry, audit: HookAuditSink | None = None): ...

    async def fire(self, event: EventType, context: HookContext) -> HookAggregate: ...

@dataclass(frozen=True)
class HookAggregate:
    """Combined result of all hooks for a single event."""
    event: str
    final_decision: Literal["allow", "block", "modify"]
    modified_payload: dict[str, Any] | None = None
    decisions: list[HookDecision] = field(default_factory=list)
    blocked_by: str | None = None  # hook_id of the first blocker
```

### § 2.4. Hook Transport — 4 типа

| Transport | Wire format | Use case |
|-----------|-------------|----------|
| **builtin** | Direct callable (async) | In-process hooks (5 builtins) |
| **subprocess** | JSON via stdin, exit 0/2 | User-defined `.py` scripts (CC convention) |
| **http** | JSON POST, response JSON | External HTTP services |
| **llm** | LLM prompt + decision | LLM-as-hook (LLMRouter DI) |

**Subprocess protocol (CC-compatible):**
```bash
# 1. Hook is invoked as: ``python hook.py <event_name>``
# 2. HookContext JSON is written to stdin (terminated by EOF or null byte)
# 3. Hook reads stdin, processes, writes HookDecision JSON to stdout
# 4. Exit code:
#    - 0 = success (allow / modify)
#    - 2 = block (stderr = reason)
#    - other = error (logged, fail-open at runner)
```

---

## § 3. 12+3 hook events — таблица trigger points и payload schema

| # | Event | Trigger point (file:line) | Payload schema | Setting (default on/off) |
|---|-------|---------------------------|----------------|--------------------------|
| 1 | `PreToolUse` | `harness/server/agent/runtime.py:ToolRuntime.execute` (line 210) | `{"tool_name": str, "tool_args": dict, "session_id": str, "agent_id": str}` | `hooks_pre_tool_use_enabled=True` |
| 2 | `PostToolUse` | Same, after `execute()` returns | `{"tool_name": str, "tool_result": {"ok": bool, "output": str, "error": str}, "duration_ms": int}` | `hooks_post_tool_use_enabled=True` |
| 3 | `Stop` | `harness/server/agent/loop.py:AgentLoop.run` (line 425) | `{"reason": "done"\|"max_iter"\|"error", "iterations": int, "total_cost": float}` | `hooks_stop_enabled=True` |
| 4 | `SubagentStart` | `harness/agents/merge_queue.py:MergeQueue._run_job` (subprocess start) + `harness/agents/runner.py:AgentRunner.run` (start) | `{"agent_name": str, "agent_id": str, "task": str, "parent_session_id": str}` | `hooks_subagent_start_enabled=True` |
| 5 | `SubagentStop` | Same, on completion (success/failure) | `{"agent_name": str, "agent_id": str, "status": "success"\|"failure", "duration_ms": int, "result_preview": str}` | `hooks_subagent_stop_enabled=True` |
| 6 | `SessionStart` | `harness/server/app.py:lifespan` startup | `{"session_id": str, "model": str, "project_root": str, "ts": float}` | `hooks_session_start_enabled=True` |
| 7 | `SessionEnd` | `harness/server/app.py:lifespan` shutdown | `{"session_id": str, "total_turns": int, "total_cost": float, "duration_s": float}` | `hooks_session_end_enabled=True` |
| 8 | `UserPromptSubmit` | `harness/server/routes/chat.py:chat_ws` (receive user message) | `{"session_id": str, "user_message": str, "model": str, "ts": float}` | `hooks_user_prompt_submit_enabled=True` |
| 9 | `PreCompact` | `harness/context/compaction.py:ContextCompactor._run_slow_path` (line 481) | `{"session_id": str, "messages_count": int, "tokens_before": int, "source_hash": str}` | `hooks_pre_compact_enabled=True` (auto-bridges `pre_compact_enabled=True`) |
| 10 | `InstructionsLoaded` | `harness/agents/spec.py:AgentSpec.parse_agent_md` (post-parse) | `{"agent_name": str, "tools": list[str], "permissions": str, "system_prompt_preview": str}` | `hooks_instructions_loaded_enabled=True` |
| 11 | `PermissionRequest` | `harness/agents/runner.py:_DeniedToolRuntime.execute` (deny override) | `{"tool_name": str, "tool_args": dict, "agent_id": str, "denied_by": str}` | `hooks_permission_request_enabled=True` |
| - | Elicitation | DEFERRED (no trigger point) | — | `hooks_elicitation_enabled=False` (Phase 4.4) |
| - | Notification | DEFERRED (no trigger point) | — | `hooks_notification_enabled=False` (Phase 4.4) |
| 12 | `OnMemoryWrite` | `harness/memory/unified.py:UnifiedMemory.write` (and `_safe_write`) | `{"layer": str, "text": str, "tags": list[str], "agent_id": str, "memory_id": str}` | `hooks_on_memory_write_enabled=True` |
| 13 | `OnRoutingDecision` | `harness/agents/router.py:LLMRouterClassifier.classify` (post-classify) | `{"task": str, "decision_agent": str, "confidence": float, "tier": str\|None, "fallback_used": bool}` | `hooks_on_routing_decision_enabled=True` |
| 14 | `OnCompaction` | `harness/context/compaction.py:ContextCompactor.maybe_compact` (post-compact) | `{"session_id": str, "strategy": "cache_hit"\|"sliding_window"\|"llm_summary", "tokens_before": int, "tokens_after": int, "summary_id": str\|None}` | `hooks_on_compaction_enabled=True` |

**Note:** 15 events, not 12+3=15. CC has 12 (excluding Elicitation+Notification which are deferred), plus 3 custom = 15. Roadmap reference says "12+3" = 15.

### § 3.1. Payload schemas (detailed)

All payloads are JSON-serialisable. Sensitive fields (PII, secrets) are **redacted by the runner** before any hook sees them (mirror of `redaction.engine.RedactionEngine`).

```python
# PreToolUse payload example
{
    "tool_name": "read_file",
    "tool_args": {"path": "src/auth.py"},
    "session_id": "sess-abc123",
    "agent_id": "explore",
    # PreCompact also includes: source_hash, model
}

# PostToolUse payload example
{
    "tool_name": "read_file",
    "tool_result": {"ok": true, "output": "...redacted...", "error": "", "exit_code": null, "duration_ms": 12},
    "duration_ms": 15,
    "session_id": "sess-abc123",
    "agent_id": "explore",
}

# Stop payload
{"reason": "done", "iterations": 3, "total_cost": 0.0123, "session_id": "sess-abc123"}

# OnMemoryWrite payload
{"layer": "L2", "text": "...redacted...", "tags": ["#compact", "session/sess-abc"], "agent_id": "explore", "memory_id": "mem-42"}

# OnRoutingDecision payload
{"task": "find the bug in auth.py", "decision_agent": "explore", "confidence": 0.92, "tier": "T1", "fallback_used": false}

# OnCompaction payload
{"session_id": "sess-abc", "strategy": "llm_summary", "tokens_before": 18000, "tokens_after": 8500, "summary_id": "compact-42"}
```

---

## § 4. Hook формат — JSON via stdin, exit 0/2

### § 4.1. Subprocess protocol

User registers a hook as a Python script:
```python
# .harness/hooks/pre_commit_check.py
import json
import sys

def main():
    context = json.load(sys.stdin)  # HookContext payload
    tool_name = context["payload"]["tool_name"]
    tool_args = context["payload"]["tool_args"]
    
    if tool_name == "bash" and "rm -rf" in tool_args.get("command", ""):
        # Block: exit 2 + reason on stderr
        print("dangerous rm -rf detected", file=sys.stderr)
        sys.exit(2)
    
    # Allow: exit 0 + optional modified payload on stdout
    print(json.dumps({"decision": "allow", "output": {}}))
    sys.exit(0)

if __name__ == "__main__":
    main()
```

Registration via Settings:
```python
hooks_subprocess_specs = 'PreToolUse:.harness/hooks/pre_commit_check.py:2000,PostToolUse:.harness/hooks/log_tool.py:1000'
```

### § 4.2. Async execution model

- `HookRunner.fire()` is `async def`. Trigger points call `await runner.fire(event, context)`.
- Each hook runs as an `asyncio.Task` created via `asyncio.create_task(self._run_hook(spec, context))`.
- `asyncio.wait_for(..., timeout=spec.timeout_ms/1000)` per-hook (settings.hooks_default_max_ms default 5000ms).
- Timeout / exception → log + audit + treat as `decision="allow"` (fail-open per Plan R3 — see § 12).
- Exception handler: `except Exception: log + return HookDecision(decision="allow", error=str(exc))`. NEVER raise.

### § 4.3. Priority + decision aggregation

```
For each hook in sorted(get(event), key=lambda h: h.priority):
    decision = await run_hook(hook, context)
    if decision.decision == "block":
        return HookAggregate(final_decision="block", blocked_by=hook.hook_id, ...)
    elif decision.decision == "modify":
        modified_payload = decision.output
    decisions.append(decision)

# No blockers → final_decision="allow" (or "modify" if any modified)
return HookAggregate(final_decision="allow"|"modify", modified_payload=modified_payload, decisions=decisions)
```

---

## § 5. 5 builtin hooks

### § 5.1. `builtin/log.py` — `LogHook`

```python
class LogHook:
    """Log every fired event at INFO level.

    Default-on (settings.hooks_log_enabled=True). Output: ``[hook] {event} session={sid} agent={aid} duration={ms}ms``.

    Use case: minimal observability without external services. Complements
    Phase 4.1 structured JSONL logging.
    """
    async def __call__(self, context: HookContext) -> HookDecision:
        logger.info("[hook] %s session=%s agent=%s",
                    context.event, context.session_id[:8], context.agent_id or "main")
        return HookDecision(hook_id="builtin.log", event=context.event, decision="allow")
```

### § 5.2. `builtin/validate.py` — `ValidateHook`

```python
class ValidateHook:
    """PreToolUse hook: enforce Pydantic schema on tool args.

    Defense-in-depth beyond TOOL_SCHEMAS (Phase 0). Catches cases where
    the LLM hallucinates an extra arg or wrong type. Default-on.

    Schema source: ``harness/server/agent/tools.py:TOOL_SCHEMAS``.
    """
    async def __call__(self, context: HookContext) -> HookDecision:
        if context.event != EventType.PRE_TOOL_USE.value:
            return HookDecision(hook_id="builtin.validate", event=context.event, decision="allow")
        tool_name = context.payload.get("tool_name")
        tool_args = context.payload.get("tool_args", {})
        schema = TOOL_SCHEMAS_BY_NAME.get(tool_name)
        if schema is None:
            return HookDecision(hook_id="builtin.validate", event=context.event, decision="allow")
        try:
            jsonschema_validate(tool_args, schema["parameters"])
        except jsonschema.ValidationError as exc:
            return HookDecision(
                hook_id="builtin.validate", event=context.event,
                decision="block", error=f"schema validation failed: {exc.message}",
            )
        return HookDecision(hook_id="builtin.validate", event=context.event, decision="allow")
```

**Note:** `jsonschema` is NOT in deps. We use stdlib `dataclasses` + custom validator OR add `jsonschema` to optional extras. **DECISION (C1):** implement minimal hand-rolled schema validator (covers `type`, `properties`, `required`, `additionalProperties: false`) — no new deps.

### § 5.3. `builtin/block_dangerous.py` — `BlockDangerousHook`

```python
class BlockDangerousHook:
    """PreToolUse hook: block obviously dangerous bash patterns.

    Defense-in-depth beyond ``is_bash_denied`` in safety.py. This hook
    runs OUTSIDE the runtime (no tool execution context), so it can
    block based on a wider regex set (e.g. ``rm -rf /``, ``curl | sh``,
    ``chmod 777``). Default-on.
    """
    DANGEROUS_PATTERNS: tuple[str, ...] = (
        r"rm\s+-rf\s+/",
        r":\(\)\s*\{.*:\|:&.*\}\s*;:",   # fork bomb
        r"curl\s+.*\|\s*(?:sudo\s+)?sh",  # curl pipe to shell
        r"chmod\s+-R\s+777\s+/",
        r"dd\s+if=.*of=/dev/(?:sda|nvme)",
        r"mkfs\.\w+\s+/dev/",
    )
    async def __call__(self, context: HookContext) -> HookDecision:
        if context.event != EventType.PRE_TOOL_USE.value:
            return HookDecision(hook_id="builtin.block_dangerous", event=context.event, decision="allow")
        tool_name = context.payload.get("tool_name")
        if tool_name != "bash":
            return HookDecision(hook_id="builtin.block_dangerous", event=context.event, decision="allow")
        command = context.payload.get("tool_args", {}).get("command", "")
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, command):
                return HookDecision(
                    hook_id="builtin.block_dangerous", event=context.event,
                    decision="block", error=f"bash matches dangerous pattern: {pattern}",
                )
        return HookDecision(hook_id="builtin.block_dangerous", event=context.event, decision="allow")
```

### § 5.4. `builtin/inject_context.py` — `InjectContextHook`

```python
class InjectContextHook:
    """UserPromptSubmit hook: prepend L0/L1 scratchpad snapshot to user message.

    Mirrors the Phase 3 v1.2.1 L0 injection (system prompt) but for
    user-side context. Reads scratchpad via duck-typed ``.read_notes()``
    on the runtime. Default-on.
    """
    async def __call__(self, context: HookContext) -> HookDecision:
        if context.event != EventType.USER_PROMPT_SUBMIT.value:
            return HookDecision(hook_id="builtin.inject_context", event=context.event, decision="allow")
        scratchpad = getattr(context, "scratchpad", None)
        if scratchpad is None:
            return HookDecision(hook_id="builtin.inject_context", event=context.event, decision="allow")
        try:
            l0_notes = await scratchpad.read_notes("L0", limit=10)
            l1_notes = await scratchpad.read_notes("L1", tag="plan", limit=1)
        except Exception:
            return HookDecision(hook_id="builtin.inject_context", event=context.event, decision="allow")
        injected = "\n".join(f"- {n.content[:200]}" for n in l0_notes)
        if l1_notes:
            injected += f"\n\n[plan] {l1_notes[0].content[:500]}"
        return HookDecision(
            hook_id="builtin.inject_context", event=context.event,
            decision="modify", output={"prepend": f"## Hot context (auto-injected by hook)\n{injected}\n\n"},
        )
```

### § 5.5. `builtin/autosave.py` — `AutosaveHook`

```python
class AutosaveHook:
    """SessionEnd hook: persist session summary to L4 (file adapter).

    Complements ``harness/memory/adapters/file.py`` (which writes
    per-message). This hook writes a session-level markdown summary
    (turns, cost, model, duration, key decisions). Default-on.
    """
    async def __call__(self, context: HookContext) -> HookDecision:
        if context.event != EventType.SESSION_END.value:
            return HookDecision(hook_id="builtin.autosave", event=context.event, decision="allow")
        session_id = context.session_id
        session_dir = Path(settings.session_dir) / f"{session_id}.md"
        session_dir.parent.mkdir(parents=True, exist_ok=True)
        body = f"# Session {session_id}\n\n"
        body += f"- turns: {context.payload.get('total_turns', 0)}\n"
        body += f"- cost: ${context.payload.get('total_cost', 0.0):.4f}\n"
        body += f"- duration: {context.payload.get('duration_s', 0.0):.1f}s\n"
        try:
            session_dir.write_text(body, encoding="utf-8")
        except OSError as exc:
            return HookDecision(hook_id="builtin.autosave", event=context.event, decision="allow", error=str(exc))
        return HookDecision(hook_id="builtin.autosave", event=context.event, decision="allow")
```

---

## § 6. Settings (15+ new) — все defaults + validators

All added to `harness/config.py:Settings` (Pydantic v2).

```python
# === Phase 4.0: Hooks framework (master switch) ===
hooks_enabled: bool = Field(
    default=True,
    description="Phase 4.0: master switch for the hooks framework. False → all hooks "
                "are no-ops (HookRunner.fire() returns allow immediately).",
)
hooks_default_max_ms: int = Field(
    default=5000, ge=100, le=60000,
    description="Phase 4.0: per-hook timeout (ms). 5000 = 5s. Each HookRunner.fire() "
                "wraps every hook in asyncio.wait_for(timeout=this/1000).",
)
hooks_audit_log: bool = Field(
    default=False,
    description="Phase 4.0: when True, every hook event + decision is mirrored to "
                "data/audit/hooks-YYYY-MM-DD.ndjson. Off by default (cheap in tests).",
)
hooks_subprocess_specs: str = Field(
    default="",
    description="Phase 4.0: comma-separated list of subprocess hooks. Format: "
                "<EventType>:<script_path>:<timeout_ms>. Example: "
                "'PreToolUse:.harness/hooks/check.py:2000,Stop:.harness/hooks/cleanup.py:1000'. "
                "Empty string = no subprocess hooks registered.",
)
hooks_http_specs: str = Field(
    default="",
    description="Phase 4.0: comma-separated list of HTTP hooks. Format: "
                "<EventType>:<url>:<timeout_ms>:<auth_header?>. Example: "
                "'PreToolUse:http://localhost:9000/hook:3000:Bearer abc123'. "
                "Empty = no HTTP hooks.",
)
hooks_llm_specs: str = Field(
    default="",
    description="Phase 4.0: comma-separated list of LLM-as-hook specs. Format: "
                "<EventType>:<model_id>:<timeout_ms>:<prompt_template>. "
                "Empty = no LLM hooks. Note: LLM hooks add latency + cost; "
                "use sparingly (e.g. for hard-to-formalise decisions).",
)
hooks_filter_chain: str = Field(
    default="",
    description="Phase 4.0: comma-separated match_glob filters applied to ALL events. "
                "Format: <field>=<pattern>. Example: 'session_id=*-prod,tool_name=!rm'. "
                "Empty = no global filter. Per-hook matchers take precedence.",
)
hooks_fail_open: bool = Field(
    default=True,
    description="Phase 4.0: when True, a hook timeout or exception is treated as "
                "decision='allow' (the operation proceeds). Set False to fail-closed "
                "(the operation is blocked). Default True (safer for chat loop).",
)
hooks_redact_payloads: bool = Field(
    default=True,
    description="Phase 4.0: when True, hook payloads are redacted via RedactionEngine "
                "BEFORE being passed to any hook (builtin, subprocess, http, llm). "
                "PII / secrets never leave the trust boundary. Default True.",
)

# === Per-event enable (15 settings) ===
hooks_pre_tool_use_enabled: bool = Field(default=True, description="Phase 4.0: enable PreToolUse event.")
hooks_post_tool_use_enabled: bool = Field(default=True, description="Phase 4.0: enable PostToolUse event.")
hooks_stop_enabled: bool = Field(default=True, description="Phase 4.0: enable Stop event.")
hooks_subagent_start_enabled: bool = Field(default=True, description="Phase 4.0: enable SubagentStart event.")
hooks_subagent_stop_enabled: bool = Field(default=True, description="Phase 4.0: enable SubagentStop event.")
hooks_session_start_enabled: bool = Field(default=True, description="Phase 4.0: enable SessionStart event.")
hooks_session_end_enabled: bool = Field(default=True, description="Phase 4.0: enable SessionEnd event.")
hooks_user_prompt_submit_enabled: bool = Field(default=True, description="Phase 4.0: enable UserPromptSubmit event.")
hooks_pre_compact_enabled: bool = Field(default=True, description="Phase 4.0: enable PreCompact event.")
hooks_instructions_loaded_enabled: bool = Field(default=True, description="Phase 4.0: enable InstructionsLoaded event.")
hooks_permission_request_enabled: bool = Field(default=True, description="Phase 4.0: enable PermissionRequest event.")
hooks_elicitation_enabled: bool = Field(default=False, description="Phase 4.0: DEFERRED. Always False in Phase 4.0.")
hooks_notification_enabled: bool = Field(default=False, description="Phase 4.0: DEFERRED. Always False in Phase 4.0.")
hooks_on_memory_write_enabled: bool = Field(default=True, description="Phase 4.0: enable OnMemoryWrite event.")
hooks_on_routing_decision_enabled: bool = Field(default=True, description="Phase 4.0: enable OnRoutingDecision event.")
hooks_on_compaction_enabled: bool = Field(default=True, description="Phase 4.0: enable OnCompaction event.")

# === Builtin hook enable (5 settings) ===
hooks_builtin_log_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin LogHook.")
hooks_builtin_validate_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin ValidateHook.")
hooks_builtin_block_dangerous_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin BlockDangerousHook.")
hooks_builtin_inject_context_enabled: bool = Field(default=False, description="Phase 4.0: enable builtin InjectContextHook (off by default — L0 already injected via Phase 3 v1.2.1).")
hooks_builtin_autosave_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin AutosaveHook.")
```

**Total new settings: 25** (1 master + 2 framework + 5 spec list + 1 filter + 2 behaviour + 15 per-event + 5 builtin = 31 actually; counted 15+ in the brief).

**Validator additions** in `_cascade_thresholds_ordered`:
```python
# Phase 4.0: hooks_default_max_ms must be sane.
if self.hooks_default_max_ms < 100:
    raise ValueError(f"hooks_default_max_ms ({self.hooks_default_max_ms}) must be >= 100")
# Phase 4.0: per-event enables for deferred events must be False.
if self.hooks_elicitation_enabled or self.hooks_notification_enabled:
    raise ValueError("hooks_elicitation_enabled and hooks_notification_enabled are DEFERRED to Phase 4.4; must be False in Phase 4.0")
```

---

## § 7. HTTP hooks — external endpoint integration

### § 7.1. Transport

`harness/hooks/http.py:HttpHookTransport`:
- **Wire format:** `POST <url>` with `Content-Type: application/json`, body = serialized `HookContext`.
- **Response:** JSON `HookDecision` (`{"decision": "allow"|"block"|"modify", "output": {...}, "error": "..."}`).
- **Timeout:** `asyncio.wait_for(spec.timeout_ms/1000)`. Transport uses `asyncio.to_thread(urllib.request.urlopen, ...)` (no httpx dep).
- **Auth:** `Authorization` header from spec.headers.
- **Error handling:** `urllib.error.HTTPError` 4xx/5xx → `decision="allow"` + log + audit. `URLError` (connection refused) → `decision="allow"` + log.

### § 7.2. Spec format

Settings string: `PreToolUse:http://localhost:9000/hook:3000:Bearer abc123`
Parsed into:
```python
HookSpec(
    hook_id="http.<auto-generated>",
    event=EventType.PRE_TOOL_USE,
    transport="http",
    url="http://localhost:9000/hook",
    method="POST",
    headers={"Authorization": "Bearer abc123"},
    timeout_ms=3000,
)
```

### § 7.3. Trust boundary

**HttpHookTransport NEVER imports `harness.agents` or `harness.server`.** Stdlib only (`urllib.request`, `asyncio`, `json`).

---

## § 8. LLM-as-hook — prompt-based decision

### § 8.1. Transport

`harness/hooks/llm_hook.py:LLMHook`:
- **Wire format:** prompt = `f"You are a hook deciding whether to {context.event}. Context: {context.payload}. Respond with JSON: {{\"decision\": \"allow\"|\"block\"|\"modify\", \"reason\": \"...\"}}"`
- **LLM call:** `await self.router.completion(messages=[{"role": "user", "content": prompt}], model=self.model)`.
- **Decision extraction:** parse response as JSON (same permissive regex as `LLMRouterClassifier._JSON_LINE_RE`).
- **Cost:** LLM hooks add latency + cost. Default OFF for built-ins; opt-in via Settings.
- **Trust boundary:** `LLMRouter` is passed via DI (constructor arg). Module does NOT import `harness.server.llm.router` at module level — `from typing import TYPE_CHECKING` + lazy import inside `__call__` (mirror of `AgentLoop._record_event` pattern from Phase 3 v1.4.0).

### § 8.2. Spec format

Settings string: `OnRoutingDecision:qwen3:8b:3000:You are a safety reviewer. Decide whether to override the routing decision...`
Parsed into:
```python
HookSpec(
    hook_id="llm.<auto-generated>",
    event=EventType.ON_ROUTING_DECISION,
    transport="llm",
    model="qwen3:8b",
    prompt="You are a safety reviewer. ...",
    timeout_ms=3000,
)
```

### § 8.3. Defence in depth

- LLM hook output is bounded (200 chars max for `reason`).
- Decision is one of 3 literals (`allow`/`block`/`modify`).
- Modify payload is bounded to 1KB.
- All LLM hook decisions are audited to NDJSON.

---

## § 9. Trust boundary — static test + enforcement

### § 9.1. Static test

`tests/test_hooks_trust_boundary.py` — mirror of `tests/eval/test_eval_trust_boundary.py`:
- Forbidden prefixes: `harness.agents`, `harness.server`.
- For each `.py` file in `harness/hooks/`, parse with `ast` and verify no `import` / `from ... import` matches the forbidden prefixes.
- `relative imports` (`.foo`) are skipped (cannot reach `harness.agents` / `harness.server` from `harness.hooks` without explicit absolute prefix).
- `TYPE_CHECKING` imports are NOT skipped — they still resolve at type-check time, so they would break the boundary. **DECISION (B1):** we use a `__getattr__` / `if TYPE_CHECKING` pattern in `llm_hook.py` to avoid any `harness.server.llm.router` import; the type is referenced as a string only.

### § 9.2. Enforcement at runtime

- `HookRunner.__init__` accepts `router: LLMRouter | None = None` (DI for LLM hooks).
- `harness/hooks/__init__.py` does NOT import `harness.agents` or `harness.server`.
- `harness/hooks/registry.py`, `runner.py`, etc. — all use stdlib + `harness.config` + `harness.redaction` (allowed, since `redaction` is read-only utility, like `eval`).

### § 9.3. Integration with production code (one-way import)

Production code imports `harness.hooks`:
```python
# harness/server/agent/runtime.py
from harness.hooks import HookRunner, HookContext, EventType
```

This is one-way (hooks ← production), NOT the reverse. Static test catches the reverse.

---

## § 10. Plan шагов (6-8 шагов с commit boundaries)

### Step 1: Foundation (events + context + registry)
- **Files:**
  - `harness/hooks/__init__.py` (public API exports)
  - `harness/hooks/events.py` (EventType enum)
  - `harness/hooks/context.py` (HookContext + HookDecision)
  - `harness/hooks/registry.py` (HookRegistry + HookSpec)
- **Tests:** `tests/test_hooks_events.py` (15 tests), `tests/test_hooks_registry.py` (20 tests).
- **Mock count target:** +35.
- **Commit message:** `feat(phase-4.0): hooks foundation (events + context + registry)`.

### Step 2: HookRunner + filter chain
- **Files:**
  - `harness/hooks/runner.py` (HookRunner + HookAggregate)
  - `harness/hooks/filter_chain.py` (match_glob-style filter)
  - `harness/hooks/audit.py` (HookAuditSink NDJSON)
- **Tests:** `tests/test_hooks_runner.py` (30 tests), `tests/test_hooks_filter.py` (15 tests), `tests/test_hooks_audit.py` (10 tests).
- **Mock count target:** +55.
- **Commit message:** `feat(phase-4.0): HookRunner + filter chain + audit`.

### Step 3: 5 builtin hooks
- **Files:**
  - `harness/hooks/builtin/__init__.py` (auto-register)
  - `harness/hooks/builtin/log.py`
  - `harness/hooks/builtin/validate.py`
  - `harness/hooks/builtin/block_dangerous.py`
  - `harness/hooks/builtin/inject_context.py`
  - `harness/hooks/builtin/autosave.py`
- **Tests:** `tests/test_hooks_builtin.py` (50 tests, 10 per hook).
- **Mock count target:** +50.
- **Commit message:** `feat(phase-4.0): 5 builtin hooks (log, validate, block_dangerous, inject_context, autosave)`.

### Step 4: Settings + trust boundary + transport (HTTP + subprocess + LLM)
- **Files:**
  - `harness/hooks/http.py` (HttpHookTransport)
  - `harness/hooks/llm_hook.py` (LLMHook)
  - `tests/test_hooks_trust_boundary.py` (static test)
  - `harness/config.py` — add 25+ new settings (Step 4 batch)
- **Tests:** `tests/test_hooks_http.py` (15 tests), `tests/test_hooks_llm.py` (10 tests), `tests/test_hooks_settings.py` (20 tests).
- **Mock count target:** +45.
- **Commit message:** `feat(phase-4.0): HTTP + LLM-as-hook transports + settings (25 new)`.

### Step 5: Wire PreToolUse + PostToolUse into ToolRuntime
- **Files:**
  - `harness/server/agent/runtime.py` — add `await self._hook_runner.fire(EventType.PRE_TOOL_USE, ctx)` at line 210 (start of `execute`) and `await self._hook_runner.fire(EventType.POST_TOOL_USE, ctx)` at line 251 (end of `execute`).
  - `harness/server/app.py:lifespan` — construct `HookRegistry` + `HookRunner` (default builtin registration), attach to `app.state.hook_runner`, pass to `ToolRuntime` constructor.
- **Tests:** `tests/test_hooks_pre_tool_use_integration.py` (15 tests), `tests/test_hooks_post_tool_use_integration.py` (10 tests).
- **Mock count target:** +25.
- **Commit message:** `feat(phase-4.0): PreToolUse + PostToolUse integrated into ToolRuntime`.

### Step 6: Wire remaining 11 events into 8 trigger points
- **Files:**
  - `harness/server/agent/loop.py:AgentLoop.run` (Stop) — line 425.
  - `harness/agents/merge_queue.py:MergeQueue._run_job` (SubagentStart, SubagentStop).
  - `harness/agents/runner.py:AgentRunner.run` (SubagentStart, SubagentStop).
  - `harness/server/app.py:lifespan` (SessionStart, SessionEnd).
  - `harness/server/routes/chat.py:chat_ws` (UserPromptSubmit).
  - `harness/context/compaction.py:ContextCompactor._run_slow_path` (PreCompact) + `maybe_compact` (OnCompaction).
  - `harness/agents/spec.py:AgentSpec.parse_agent_md` (InstructionsLoaded).
  - `harness/memory/unified.py:UnifiedMemory.write` (OnMemoryWrite).
  - `harness/agents/router.py:LLMRouterClassifier.classify` (OnRoutingDecision).
  - `harness/agents/runner.py:_DeniedToolRuntime.execute` (PermissionRequest).
- **Tests:** `tests/test_hooks_events_integration.py` (40 tests).
- **Mock count target:** +40.
- **Commit message:** `feat(phase-4.0): 11 events integrated into 8 trigger points`.

### Step 7: Backward compat with PreCompactHook
- **Files:**
  - `harness/context/compaction.py:ContextCompactor.__init__` — if `pre_compact_hook` is set, auto-register a `OnPreCompact` wrapper in the HookRegistry.
  - `harness/agents/pre_compact.py:PreCompactHook` — no changes (zero-touch backward compat).
  - `tests/test_hooks_pre_compact_compat.py` (10 tests).
- **Mock count target:** +10.
- **Commit message:** `feat(phase-4.0): backward compat with PreCompactHook (Phase 3 v1.5.0)`.

### Step 8: Documentation + tag
- **Files:**
  - `docs/hooks.md` (user-facing docs, ~300 lines)
  - `docs/PHASE4-HOOKS-PLAN.md` (this file, move from plan to spec)
  - `CHANGELOG.md` (v1.6.0 entry)
  - `README.md` (update Phase 4 status)
  - `docs/roadmap.md` → master — DO NOT EDIT. Note: master update is the post-coding step (separate ticket).
- **Tests:** `tests/test_hooks_docs_examples.py` (5 smoke tests — verify all code examples in docs/hooks.md actually work).
- **Mock count target:** +5.
- **Commit message:** `feat(phase-4.0): docs/hooks.md + v1.6.0 changelog`.

**Total mock tests added: 35 + 55 + 50 + 45 + 25 + 40 + 10 + 5 = 265 new tests.**

**Total cumulative mock tests: 1505 → 1770 (well above Mark's 1500 floor).**

---

## § 11. Definition of Done (Phase 4.0)

### Code
- [ ] `harness/hooks/` package created with 9 production modules (events/context/registry/runner/filter_chain/audit + 5 builtin).
- [ ] `harness/config.py` has 25+ new settings with default + Pydantic validator.
- [ ] 9 trigger points fire hooks: ToolRuntime (Pre+PostToolUse), AgentLoop (Stop), AgentRunner (SubagentStart/Stop), MergeQueue (SubagentStart/Stop), app.lifespan (SessionStart/End), chat.ws (UserPromptSubmit), compaction (PreCompact+OnCompaction), spec.parse (InstructionsLoaded), memory.write (OnMemoryWrite), router.classify (OnRoutingDecision), denylist (PermissionRequest).
- [ ] PreCompactHook auto-registered when compactor has `pre_compact_hook` set (backward compat).
- [ ] 5 builtin hooks: log, validate, block_dangerous, inject_context, autosave.
- [ ] 4 transports: builtin, subprocess (JSON via stdin, exit 0/2), http (urllib), llm (DI to LLMRouter).

### Tests
- [ ] 265+ new mock tests.
- [ ] `tests/test_hooks_trust_boundary.py` passes (forbidden imports = 0).
- [ ] `pytest tests/` passes 100% (0 regressions).
- [ ] Mock suite total >= 1500 (cumulative).
- [ ] 5 smoke tests in `test_hooks_docs_examples.py` (all code examples in docs work).

### Backward compat
- [ ] `PreCompactHook` (Phase 3 v1.5.0) works unchanged.
- [ ] All Phase 3 settings (compaction_*, reflection_*, scratchpad_*, redaction_*, privacy_*) unchanged.
- [ ] No breaking changes to `AgentLoop` / `ToolRuntime` / `AgentRunner` public APIs.

### Docs
- [ ] `docs/hooks.md` exists (~300 lines, user-facing).
- [ ] `docs/PHASE4-HOOKS-PLAN.md` exists (this file).
- [ ] `CHANGELOG.md` has v1.6.0 entry.
- [ ] Master roadmap (`docs/roadmap.md`) is updated POST-coding (separate ticket — DO NOT include in Phase 4.0 PR).

### Git
- [ ] 8 commits (one per step).
- [ ] Branch: `feat/phase-4-hooks` (off master).
- [ ] PR opened against master.
- [ ] Tag v1.6.0 on merge.

### Adversarial review
- [ ] 5+ BLOCKERS, 5+ RISKS, 5+ CONCERNS identified (§ 12).
- [ ] All BLOCKERS fixed before merge.
- [ ] All RISKS tracked in GH issues (deferred to Phase 4.0.x).

---

## § 12. Adversarial review (5+ BLOCKERS, 5+ RISKS, 5+ CONCERNS)

### BLOCKERS (B) — must be fixed before merge

| ID | Category | Description | Fix |
|----|----------|-------------|-----|
| **B1** | Trust boundary | `llm_hook.py` needs `LLMRouter` for completion calls. Direct import breaks trust boundary. | Use DI: `LLMHook(router=...)` constructor arg. Module-level: `from typing import TYPE_CHECKING` + `_get_router_type()` helper. No `harness.server.llm.router` import at runtime. |
| **B2** | Trust boundary | `harness/hooks/audit.py` may want to import `harness.context.scratchpad_audit.ScratchpadAudit` for unified audit. BREAKS trust boundary. | Mirror pattern: `HookAuditSink` writes its own NDJSON (stdlib `json` + `pathlib`). No `harness.*` import beyond `harness.config` (allowed) + `harness.redaction` (allowed, read-only utility). |
| **B3** | Subprocess protocol | CC convention is `python hook.py <event>` (event as argv). Our spec format is `PreToolUse:script.py:2000` (no event in argv). Inconsistent with CC docs. | Update spec format: `<EventType>:<script_path>:<timeout_ms>` — runner passes event as argv[1] AND as JSON stdin field. Hook scripts can read either. **DEFERRED FIX:** for Phase 4.0, the JSON stdin field is the only contract (argv[1] is optional convenience). |
| **B4** | HTTP timeout | `urllib.request.urlopen` has NO native timeout when used via `asyncio.to_thread`. Must wrap in `concurrent.futures` + `Future.result(timeout=...)`. | Use `asyncio.wait_for(asyncio.to_thread(urllib.request.urlopen, ...), timeout=spec.timeout_ms/1000)`. |
| **B5** | Reentrancy | `UnifiedMemory.write` already has a `OnMemoryWrite` hook wired. If a hook writes to memory (e.g. autosave to L4), it triggers `OnMemoryWrite` recursively. Infinite loop. | Add `ctx.recursion_depth` field. `HookRunner.fire()` checks `ctx.recursion_depth < settings.hooks_max_recursion_depth` (default 3) and short-circuits to `decision="allow"` if exceeded. |
| **B6** | Schema validator | `jsonschema` is not in deps. Plan C1 says "hand-rolled minimal validator" — but Pydantic is already a dep. Use Pydantic. | Use `pydantic.TypeAdapter(schema["parameters"]["$defs"]["..."]).validate_python(tool_args)` if Pydantic schema; else fall back to simple type-check. **SIMPLER:** convert TOOL_SCHEMAS to Pydantic models at load time in `TOOL_SCHEMAS_BY_NAME: dict[str, type[BaseModel]]` (lazy). |
| **B7** | Compactor integration | Phase 3 v1.5.0's `PreCompactHook` is `async def __call__(*, session_id, messages, metadata) -> PreCompactState | None`. Our `OnPreCompact` is `async def __call__(context: HookContext) -> HookDecision`. Signature mismatch. | Auto-wrap: `PreCompactHookAdapter(pre_compact_hook: PreCompactHook)` translates between signatures. |

### RISKS (R) — may go wrong, mitigated

| ID | Description | Mitigation |
|----|-------------|------------|
| **R1** | Hook subprocess hangs > timeout → asyncio.wait_for cancels, but Python subprocess doesn't always die on task cancel. | Use `asyncio.create_subprocess_exec` with `preexec_fn=os.setsid` (Unix) or `subprocess.CREATE_NEW_PROCESS_GROUP` (Windows); cancel the entire process group on timeout. Phase 4.0 ships Unix-only mitigation; Windows mitigation in Phase 4.0.1. |
| **R2** | Hook HTTP call returns 5xx → runner returns `decision="allow"` (fail-open). User may want fail-closed. | `settings.hooks_fail_open` (default True). Set False for production. Document trade-off in `docs/hooks.md`. |
| **R3** | LLM-as-hook adds latency per call (1-10s) → chat loop blocks. | Per-hook timeout (default 3000ms). If LLM hook times out, runner returns `decision="allow"` (fail-open) and logs warning. |
| **R4** | Hook chain explosion: 15 events × 5 builtins + N user hooks = 50+ async tasks per chat turn. | `asyncio.gather` with `return_exceptions=True`. Per-event cap: `settings.hooks_max_per_event` (default 10). Hooks beyond cap are silently dropped + logged. |
| **R5** | `OnMemoryWrite` fires for EVERY memory write (including hook-internal ones like the pre-compact snapshot). Volume explosion. | Mirror the recursion guard (B5). Also: `hooks_on_memory_write_silent_layers` setting (default `["L1"]` for hmem — L1 is hand-curated, no auto-audit). |
| **R6** | `SessionStart` / `SessionEnd` fire on FastAPI startup/shutdown, but multi-worker deployments (uvicorn --workers 4) get 4× events. | Phase 4.0 = single-worker. Document multi-worker limitation in `docs/hooks.md`. Phase 4.0.1 adds worker-id tag. |
| **R7** | Subprocess hook may be malicious (write to filesystem, exfiltrate secrets). | Settings allowlist: `settings.hooks_subprocess_allowed_paths` (default `.harness/hooks/**`). Reject hook scripts outside the allowlist. |
| **R8** | Recursive hooks: hook A triggers tool call → PreToolUse fires again → hook A runs again. Infinite loop. | Per-event reentrancy guard: `HookRunner` tracks `event_stack: list[EventType]`. If event already in stack, skip. |

### CONCERNS (C) — code quality, not blocking

| ID | Description | Resolution |
|----|-------------|------------|
| **C1** | Hand-rolled schema validator for ValidateHook duplicates Pydantic's logic. | Use Pydantic (see B6 fix). |
| **C2** | `HookContext` is a frozen dataclass; `decision="modify"` requires creating a new context. Awkward. | Add `HookContext.with_payload(new_payload: dict) -> HookContext` helper method. |
| **C3** | `hooks_subprocess_specs` / `hooks_http_specs` / `hooks_llm_specs` parsing duplicated. | Extract `parse_hook_specs(s: str, transport: str) -> list[HookSpec]` helper. |
| **C4** | `harness/hooks/__init__.py` will re-export 20+ symbols. Wildcard import discouraged. | Explicit `__all__` list. `from harness.hooks import HookRunner, EventType, ...` (no wildcard). |
| **C5** | Tests may import from `harness.hooks.builtin` to test individual hooks. This is fine for tests (they can import anything). Document this. | README note: "Tests may import from `harness.hooks.*` freely; production code imports only from `harness.hooks` (top-level)." |
| **C6** | `HookAuditSink` writes to `data/audit/hooks-YYYY-MM-DD.ndjson`. Path is not configurable. | Add `settings.hooks_audit_dir` (default `<project_root>/data/audit`). |
| **C7** | `PermissionRequest` semantics unclear: does it ASK the user (interactive), or is it auto-allowed/denied? Phase 3 currently has denylist (auto). | Phase 4.0 = synchronous allow/deny (no interactive prompt). Interactive prompt deferred to Phase 4.4 with Elicitation. |
| **C8** | `OnRoutingDecision` fires AFTER the decision is made (post-classify). "On" semantics: read-only? | Yes, read-only. Document in `docs/hooks.md`. Use `decision="modify"` to override (set `decision_agent` in `output`). |
| **C9** | `OnCompaction` fires for cache-hit too. Volume. | Filter: `settings.hooks_on_compaction_skip_cache_hit` (default True). Skip cache-hit events unless False. |
| **C10** | Builtin hooks are in `harness/hooks/builtin/` (lowercase, plural). Roadmap says `harness/hooks/builtin/`. Consistent. | — |

---

## § 13. Файлы (новые/изменённые) — full file tree

### NEW files
```
harness/hooks/
├── __init__.py                                  # Public API
├── events.py                                    # EventType enum
├── context.py                                   # HookContext, HookDecision
├── registry.py                                  # HookRegistry, HookSpec
├── runner.py                                    # HookRunner, HookAggregate
├── filter_chain.py                              # match_glob filter
├── audit.py                                     # HookAuditSink (NDJSON)
├── http.py                                      # HttpHookTransport
├── llm_hook.py                                  # LLMHook (DI to LLMRouter)
└── builtin/
    ├── __init__.py                              # Auto-register
    ├── log.py
    ├── validate.py
    ├── block_dangerous.py
    ├── inject_context.py
    └── autosave.py

tests/
├── test_hooks_events.py
├── test_hooks_registry.py
├── test_hooks_runner.py
├── test_hooks_filter.py
├── test_hooks_audit.py
├── test_hooks_builtin.py
├── test_hooks_http.py
├── test_hooks_llm.py
├── test_hooks_settings.py
├── test_hooks_pre_tool_use_integration.py
├── test_hooks_post_tool_use_integration.py
├── test_hooks_events_integration.py
├── test_hooks_pre_compact_compat.py
├── test_hooks_docs_examples.py
└── test_hooks_trust_boundary.py                 # CRITICAL: trust boundary test

docs/
├── hooks.md                                      # User-facing docs
└── PHASE4-HOOKS-PLAN.md                          # This file
```

### MODIFIED files
```
harness/config.py                                # +25 settings
harness/server/agent/runtime.py                  # Wire PreToolUse + PostToolUse (Step 5)
harness/server/agent/loop.py                     # Wire Stop (Step 6)
harness/agents/runner.py                         # Wire SubagentStart/Stop + PermissionRequest (Step 6)
harness/agents/merge_queue.py                    # Wire SubagentStart/Stop (Step 6)
harness/server/app.py                            # Wire SessionStart/End + construct HookRunner (Step 6)
harness/server/routes/chat.py                    # Wire UserPromptSubmit (Step 6)
harness/context/compaction.py                    # Wire PreCompact + OnCompaction + PreCompactHook compat (Steps 6, 7)
harness/agents/spec.py                           # Wire InstructionsLoaded (Step 6)
harness/memory/unified.py                        # Wire OnMemoryWrite (Step 6)
harness/agents/router.py                         # Wire OnRoutingDecision (Step 6)
CHANGELOG.md                                     # v1.6.0 entry
README.md                                        # Phase 4 status update
```

### UNCHANGED files
```
harness/agents/pre_compact.py                    # ZERO TOUCH (backward compat)
harness/observability/                           # Scaffold only, no changes
master roadmap                                   # Read-only, updated post-coding
```

### Total file count
- NEW: 23 (11 source + 11 tests + 1 trust test + ... actually 14 source + 14 tests = 28; let me recount: 9 hooks source + 5 builtin source = 14 hooks source; 14 test files = 14; 2 docs = 2 → total 30 NEW files)
- MODIFIED: 12 (1 config + 10 production + 1 CHANGELOG + 1 README = 13 actually; let me recount: 1 config + 10 production = 11; + 2 docs = 13 MODIFIED)
- UNCHANGED: 2 (pre_compact.py + observability/)

---

## § 14. Стек — что добавляем, что НЕ добавляем

### ДОБАВЛЯЕМ (zero new required deps)
- `asyncio.wait_for` (stdlib) — per-hook timeout
- `asyncio.create_subprocess_exec` (stdlib) — subprocess transport
- `asyncio.create_task` (stdlib) — concurrent hook execution
- `asyncio.gather` (stdlib) — parallel hook dispatch
- `urllib.request` (stdlib) — HTTP transport (no httpx)
- `json` (stdlib) — JSON via stdin/stdout + payload serialisation
- `subprocess` (stdlib) — sync fallback (debug only)
- `hmac` (stdlib) — optional HMAC signing for HTTP hooks (Phase 4.0.1)
- `dataclasses` (stdlib) — HookContext, HookDecision, HookSpec, HookAggregate
- `enum` (stdlib) — EventType
- `pathlib` (stdlib) — audit log paths
- `hashlib` (stdlib) — payload hash for audit
- `time` (stdlib) — timestamps
- `logging` (stdlib) — log builtin
- `re` (stdlib) — block_dangerous patterns
- `typing` (stdlib) — type hints
- `ast` (stdlib) — static test for trust boundary

### Existing deps USED
- `pydantic` (existing) — Settings, ToolRuntime pydantic models
- `pydantic_settings` (existing) — Settings class
- `aiosqlite` (existing) — session DB
- `fastapi` (existing) — Server (unchanged)

### НЕ добавляем (explicitly OUT)
- `httpx` — use stdlib `urllib.request` instead (Plan B4)
- `jsonschema` — use Pydantic (Plan B6)
- `watchfiles` / `watchdog` — hot-reload deferred to Phase 4.2
- `opentelemetry` / `opentelemetry-api` — observability deferred to Phase 4.1
- `prometheus-client` — metrics deferred to Phase 4.1
- `pyyaml` — use existing frontmatter parser
- `click` / `typer` — CLI deferred to Phase 4.5
- `tenacity` — use stdlib retry (Phase 4.0.1)

### Optional extras (NOT required for Phase 4.0)
- `[hooks_http_auth]` — `cryptography` for HMAC signing (Phase 4.0.1)

---

## § 15. Поэтапная сводка

| Step | Commit | Files | Tests | Cumulative |
|------|--------|-------|-------|-----------|
| 1 | Foundation | 3 new | +35 | 1505 → 1540 |
| 2 | Runner + filter + audit | 3 new | +55 | 1540 → 1595 |
| 3 | 5 builtin hooks | 6 new | +50 | 1595 → 1645 |
| 4 | HTTP + LLM + settings + trust | 4 new + 1 mod | +45 | 1645 → 1690 |
| 5 | PreToolUse + PostToolUse integration | 2 mod | +25 | 1690 → 1715 |
| 6 | 11 events × 8 trigger points | 10 mod | +40 | 1715 → 1755 |
| 7 | PreCompactHook compat | 1 mod | +10 | 1755 → 1765 |
| 8 | Docs + tag v1.6.0 | 2 new + 2 mod | +5 | 1765 → 1770 |

**Total:** 8 commits, +265 tests, 0 new required deps, 0 breaking changes.

---

## § 16. Заключение

Phase 4.0 — это **foundation** phase. Цель: дать пользователю возможность вмешиваться в lifecycle агента через 15 hook-событий (12 CC + 3 custom) с 4 транспортами (builtin/subprocess/http/llm), не ломая backward compat с Phase 3 v1.5.0. Trust boundary строго изолирован: `harness/hooks/` не импортирует production code. Static test это проверяет.

Все 7 BLOCKERS идентифицированы и имеют фиксы в § 12. Все 8 RISKS трекаются. Все 10 CONCERNS — code quality, не блокеры.

После coding — `feat/phase-4-hooks` PR → review → merge → tag v1.6.0 → update master roadmap (`docs/roadmap.md` Phase 4 = 12/12 FINAL).

**Next phase (4.1):** observability stack (structured JSONL + OTel + Prometheus + health checks).
**Next phase (4.2):** hot-reload hooks + agents via file watcher.

---

**End of plan.**
