"""Phase 4.0: HookRunner — async dispatch with timeout + recursion guard.

The runner is the single entry point for production code. It:
    1. Resolves enabled hooks for the event (registry.for_event).
    2. Filters by per-spec matcher + global hooks_filter_chain.
    3. Dispatches each hook in parallel via ``asyncio.gather`` (with
       per-hook timeout via ``asyncio.wait_for``).
    4. Aggregates decisions (block > modify > allow).
    5. Returns ``HookAggregate`` for downstream code.

All 4 transports supported (Phase 4.0 Step 3): builtin, subprocess,
http, llm. LLM router is injected via DI to maintain the trust
boundary (no module-level import of ``harness.server.llm.router``).

Trust boundary: stdlib + asyncio + dataclasses. NO ``harness.agents``
or ``harness.server`` imports.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from harness.hooks.context import (
    Decision,
    HookAggregate,
    HookContext,
    HookDecision,
    validate_payload,
)
from harness.hooks.events import EventType
from harness.hooks.filter_chain import matches_filter_chain
from harness.hooks.registry import HookRegistry, HookSpec


logger = logging.getLogger(__name__)


async def _invoke_builtin(
    spec: HookSpec,
    context: HookContext,
    *,
    timeout_ms: int,
) -> HookDecision:
    """Invoke a single builtin hook with timeout. Always returns a decision."""
    if spec.callable is None:
        return HookDecision(
            decision="allow",
            hook_id=spec.hook_id,
            error="builtin spec has no callable",
        )
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            spec.callable(context),
            timeout=timeout_ms / 1000.0,
        )
        duration_ms = (time.monotonic() - start) * 1000.0
        if not isinstance(result, HookDecision):
            return HookDecision(
                decision="allow",
                hook_id=spec.hook_id,
                duration_ms=duration_ms,
                error=f"hook returned {type(result).__name__}, expected HookDecision",
            )
        return HookDecision(
            decision=result.decision,
            hook_id=spec.hook_id,
            duration_ms=duration_ms,
            output=result.output,
            error=result.error,
        )
    except asyncio.TimeoutError:
        duration_ms = (time.monotonic() - start) * 1000.0
        logger.warning(
            "Hook %s timed out after %dms", spec.hook_id, int(duration_ms)
        )
        return HookDecision(
            decision="allow",
            hook_id=spec.hook_id,
            duration_ms=duration_ms,
            error=f"timeout after {timeout_ms}ms",
        )
    except Exception as e:  # noqa: BLE001
        duration_ms = (time.monotonic() - start) * 1000.0
        logger.warning(
            "Hook %s raised %s: %s", spec.hook_id, type(e).__name__, e
        )
        return HookDecision(
            decision="allow",
            hook_id=spec.hook_id,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
        )


class HookRunner:
    """Async dispatcher for registered hooks.

    Construction takes a ``HookRegistry`` and a default timeout. The
    runner is stateless except for the registry reference + optional
    ``llm_router`` (DI for LLM-as-hook transport), so the same
    instance can serve all sessions / agents.

    Example::

        registry = HookRegistry()
        await registry.register(HookSpec(
            hook_id="h1", event=EventType.PRE_TOOL_USE,
            transport="builtin", callable=my_hook,
        ))
        runner = HookRunner(registry, default_timeout_ms=3000)
        ctx = HookContext(event="PreToolUse", session_id="s1", agent_id="",
                          payload={"tool_name": "read_file"})
        agg = await runner.fire(ctx)
        if agg.final_decision == "block":
            raise RuntimeError(agg.blocked_by)
    """

    def __init__(
        self,
        registry: HookRegistry,
        *,
        default_timeout_ms: int = 3000,
        max_per_event: int = 10,
        max_recursion_depth: int = 3,
        fail_open: bool = True,
        global_filter: str = "",
        llm_router: Any = None,
        audit_sink: Any = None,
    ) -> None:
        self._registry = registry
        self._default_timeout_ms = default_timeout_ms
        self._max_per_event = max_per_event
        self._max_recursion_depth = max_recursion_depth
        self._fail_open = fail_open
        self._global_filter = global_filter
        # Optional DI for LLM-as-hook transport (B1: keeps trust boundary).
        self._llm_router = llm_router
        # Optional audit sink (DI; defaults to None = no audit).
        self._audit_sink = audit_sink

    async def fire(self, context: HookContext) -> HookAggregate:
        """Dispatch all hooks for ``context.event``.

        Returns a ``HookAggregate``. If no hooks are registered for the
        event, returns ``allow`` with empty decisions.

        Recursion guard: if ``context.recursion_depth`` exceeds
        ``max_recursion_depth``, short-circuits to ``allow``.

        Phase 4.6 v1.16.0: the payload is validated against the
        per-event Pydantic schema before dispatch. Validation is
        fail-open — on a schema mismatch, the ORIGINAL payload is
        used and a warning is logged. Hook dispatch is never broken
        by a schema regression.
        """
        import time as _time
        from harness.observability import emit_hook_dispatch

        # Phase 4.6 v1.16.0: advisory payload validation (fail-open).
        # On validation error, validate_payload logs a warning and
        # returns the original payload — we use that original.
        validated_payload = validate_payload(
            context.event, context.payload
        )
        if validated_payload is not context.payload:
            # Validation succeeded and produced a (possibly normalised)
            # dict — update the context for downstream hooks.
            context = context.with_payload(validated_payload)

        _start = _time.monotonic()
        aggregate = await self._fire_impl(context)
        # Phase 4.1 Step 6.5: emit hook dispatch metric + log AFTER
        # the aggregate is built. decision ∈ {allow, block, modify}.
        try:
            emit_hook_dispatch(
                event=context.event,
                decision=aggregate.final_decision,
                duration_s=_time.monotonic() - _start,
                hook_name=aggregate.blocked_by or "",
                request_id=context.request_id or "",
            )
        except Exception:  # noqa: BLE001 — observability must never break hooks
            logger.debug("emit_hook_dispatch failed for %s", context.event, exc_info=True)
        return aggregate

    async def _fire_impl(self, context: HookContext) -> HookAggregate:
        if context.recursion_depth >= self._max_recursion_depth:
            logger.debug(
                "Hook recursion depth %d exceeded %d — short-circuit allow",
                context.recursion_depth,
                self._max_recursion_depth,
            )
            return HookAggregate(final_decision="allow", decisions=())

        if context.event in context.event_stack:
            logger.debug(
                "Hook reentrancy detected for %s in stack %s — skip",
                context.event,
                context.event_stack,
            )
            return HookAggregate(final_decision="allow", decisions=())

        specs = self._registry.for_event(EventType(context.event))
        matching: list[HookSpec] = []
        for s in specs:
            if not s.enabled:
                continue
            if s.matcher and not matches_filter_chain(
                s.matcher,
                event=context.event,
                session_id=context.session_id,
                agent_id=context.agent_id,
                payload=context.payload,
                request_id=context.request_id,
            ):
                continue
            if not matches_filter_chain(
                self._global_filter,
                event=context.event,
                session_id=context.session_id,
                agent_id=context.agent_id,
                payload=context.payload,
                request_id=context.request_id,
            ):
                continue
            matching.append(s)
            if len(matching) >= self._max_per_event:
                logger.warning(
                    "Hook cap reached for %s: %d (dropping rest)",
                    context.event,
                    self._max_per_event,
                )
                break

        if not matching:
            return HookAggregate(final_decision="allow", decisions=())

        results = await asyncio.gather(
            *(self._dispatch_one(s, context) for s in matching),
            return_exceptions=False,
        )

        # Aggregate: first block wins; last modify wins for payload.
        final_decision: Decision = "allow"
        final_payload = dict(context.payload)
        blocked_by = ""
        decisions: list[HookDecision] = []
        for s, r in zip(matching, results):
            decisions.append(r)
            if r.decision == "block":
                final_decision = "block"
                if not blocked_by:
                    blocked_by = s.hook_id
            elif r.decision == "modify":
                final_decision = "modify"
                if r.output.get("payload"):
                    final_payload = dict(r.output["payload"])

        if not self._fail_open and any(d.error for d in decisions):
            if final_decision == "allow":
                final_decision = "block"
                blocked_by = decisions[0].hook_id

        aggregate = HookAggregate(
            final_decision=final_decision,
            decisions=tuple(decisions),
            final_payload=final_payload,
            blocked_by=blocked_by,
        )

        # Optional audit (best-effort, never raises).
        if self._audit_sink is not None:
            try:
                self._audit_sink.record(
                    aggregate=aggregate,
                    event=context.event,
                    session_id=context.session_id,
                    agent_id=context.agent_id,
                    request_id=context.request_id,
                )
            except Exception:  # noqa: BLE001
                pass

        return aggregate

    async def _dispatch_one(
        self,
        spec: HookSpec,
        context: HookContext,
    ) -> HookDecision:
        """Dispatch a single hook by transport (Step 3: all 4)."""
        timeout = spec.timeout_ms or self._default_timeout_ms
        if spec.transport == "builtin":
            return await _invoke_builtin(spec, context, timeout_ms=timeout)
        if spec.transport == "subprocess":
            from harness.hooks.subprocess import invoke_subprocess_hook

            return await invoke_subprocess_hook(
                spec.script_path, context, timeout_ms=timeout
            )
        if spec.transport == "http":
            from harness.hooks.http import invoke_http_hook

            return await invoke_http_hook(
                spec.url,
                context,
                timeout_ms=timeout,
                headers=spec.headers,
            )
        if spec.transport == "llm":
            if self._llm_router is None:
                return HookDecision(
                    decision="allow",
                    hook_id=spec.hook_id,
                    error="LLM hook requested but runner.llm_router is None",
                )
            from harness.hooks.llm_hook import LLMHook

            hook = LLMHook(
                router=self._llm_router,
                model=spec.model,
                prompt=spec.prompt,
                timeout_ms=timeout,
            )
            return await hook(context)
        return HookDecision(
            decision="allow",
            hook_id=spec.hook_id,
            error=f"unknown transport {spec.transport!r}",
        )


__all__ = ["HookRunner", "_invoke_builtin", "safe_fire"]


# === Phase 4.4+ v1.14.0: process-level singleton for production emission sites ===

_global_runner: "HookRunner | None" = None


def get_global_hook_runner() -> "HookRunner":
    """Return the process-level HookRunner singleton.

    Production emission sites (AgentLoop, AgentRunner, ContextCompactor,
    UnifiedMemory, LLMRouterClassifier) call this when they don't have
    access to a DI'd runner. The runner is bound to the same registry
    that ``get_registry()`` returns (Phase 4.4 singleton with builtins
    loaded), so registration via ``.harness/hooks/*.json`` is visible
    to all emission points.

    The server ALSO exposes its own runner on ``app.state.hook_runner``
    (initialized in ``lifespan`` with the same registry). Production
    code that already has DI (e.g. ``runtime.ToolRuntime``) should
    prefer the injected runner; this singleton is the fallback for
    sites that cannot be DI'd without breaking the trust boundary.

    Trust boundary: this module is stdlib + asyncio + dataclasses.
    It imports from ``harness.hooks.*`` and ``harness.observability``
    ONLY. It does NOT import from ``harness.agents`` or
    ``harness.server`` (enforced by
    ``tests/test_hooks_trust_boundary.py``).
    """
    global _global_runner
    if _global_runner is None:
        from harness.hooks.registry import get_registry
        _global_runner = HookRunner(get_registry(), default_timeout_ms=3000)
    return _global_runner


def set_global_hook_runner(runner: "HookRunner | None") -> None:
    """Inject a runner (DI from app.state) or reset the singleton.

    Called by ``lifespan`` to bind the server's runner to the
    global handle so production sites without DI get the same
    registry. Pass ``None`` to reset (tests + shutdown).
    """
    global _global_runner
    _global_runner = runner


async def safe_fire(
    event: str,
    *,
    session_id: str = "",
    agent_id: str = "",
    payload: dict | None = None,
    request_id: str = "",
) -> Decision:
    """Fire a hook event with full failure isolation.

    Production call sites should use this wrapper, not ``runner.fire()``
    directly. ``safe_fire``:
      - Builds the ``HookContext``.
      - Calls ``get_global_hook_runner().fire(ctx)``.
      - Catches ALL exceptions (unknown event name, runner crash,
        observer failure) and returns ``"allow"`` (fail-open).
      - Never raises.

    The returned decision is what the caller should respect:
      - ``"allow"`` — proceed as normal.
      - ``"modify"`` — proceed, but the caller's payload MAY be
        different (see ``aggregate.final_payload``). For v1.14.0
        the returned decision is sufficient; payload-mutation
        per-event is a Phase 4.5 concern.
      - ``"block"`` — caller should abort the operation. Some
        events (Stop, SessionEnd, OnCompaction) cannot be truly
        aborted and are documented as "best-effort, log only".
    """
    from harness.hooks.context import HookContext
    ctx = HookContext(
        event=event,
        session_id=session_id,
        agent_id=agent_id,
        payload=payload or {},
        request_id=request_id,
    )
    try:
        runner = get_global_hook_runner()
        agg = await runner.fire(ctx)
        return agg.final_decision
    except Exception as e:  # noqa: BLE001 — production must never crash on hook failure
        logger.debug(
            "safe_fire(%s) swallowed exception: %s: %s",
            event, type(e).__name__, e,
        )
        return "allow"
