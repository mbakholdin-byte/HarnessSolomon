"""Phase 4.0: HookRunner — async dispatch with timeout + recursion guard.

The runner is the single entry point for production code. It:
    1. Resolves enabled hooks for the event (registry.for_event).
    2. Filters by per-spec matcher + global hooks_filter_chain.
    3. Dispatches each hook in parallel via ``asyncio.gather`` (with
       per-hook timeout via ``asyncio.wait_for``).
    4. Aggregates decisions (block > modify > allow).
    5. Returns ``HookAggregate`` for downstream code.

Builtin transport is the simplest: just calls ``spec.callable(context)``.
Subprocess / HTTP / LLM transports are dispatched via the same
interface — Step 2 / Step 3.

Trust boundary: stdlib + asyncio + dataclasses. NO ``harness.agents``
or ``harness.server`` imports. LLM hook router is injected via DI
when the LLM transport is used (see ``llm_hook.py``, deferred to Step 3).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from harness.hooks.context import (
    Decision,
    HookAggregate,
    HookContext,
    HookDecision,
)
from harness.hooks.events import EventType
from harness.hooks.filter_chain import matches_filter_chain
from harness.hooks.registry import HookRegistry, HookSpec


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BuiltinResult:
    """Result of dispatching a single builtin hook."""

    hook_id: str
    decision: Decision
    output: dict[str, Any]
    error: str
    duration_ms: float


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
        # Preserve duration measured here.
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
            decision="allow",  # fail-open by default
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
            decision="allow",  # fail-open by default
            hook_id=spec.hook_id,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
        )


class HookRunner:
    """Async dispatcher for registered hooks.

    Construction takes a ``HookRegistry`` and a default timeout. The
    runner is stateless except for the registry reference, so the
    same instance can serve all sessions / agents.

    Example::

        registry = HookRegistry()
        registry.register(HookSpec(hook_id="h1", event=EventType.PRE_TOOL_USE,
                                    transport="builtin", callable=my_hook))
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
    ) -> None:
        self._registry = registry
        self._default_timeout_ms = default_timeout_ms
        self._max_per_event = max_per_event
        self._max_recursion_depth = max_recursion_depth
        self._fail_open = fail_open
        self._global_filter = global_filter

    async def fire(self, context: HookContext) -> HookAggregate:
        """Dispatch all hooks for ``context.event``.

        Returns a ``HookAggregate``. If no hooks are registered for the
        event, returns ``allow`` with empty decisions.

        Recursion guard: if ``context.recursion_depth`` exceeds
        ``max_recursion_depth``, short-circuits to ``allow``.
        """
        if context.recursion_depth >= self._max_recursion_depth:
            logger.debug(
                "Hook recursion depth %d exceeded %d — short-circuit allow",
                context.recursion_depth,
                self._max_recursion_depth,
            )
            return HookAggregate(final_decision="allow", decisions=())

        # Reentrancy guard: skip if event is already in the stack.
        if context.event in context.event_stack:
            logger.debug(
                "Hook reentrancy detected for %s in stack %s — skip",
                context.event,
                context.event_stack,
            )
            return HookAggregate(final_decision="allow", decisions=())

        specs = self._registry.for_event(EventType(context.event))
        # Filter: global filter + per-spec matcher.
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

        # Dispatch in parallel.
        results = await asyncio.gather(
            *(
                self._dispatch_one(s, context)
                for s in matching
            ),
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

        # If the runner is fail-closed and any hook errored, downgrade
        # the final decision to block.
        if not self._fail_open and any(d.error for d in decisions):
            if final_decision == "allow":
                final_decision = "block"
                blocked_by = decisions[0].hook_id  # any error blocks

        return HookAggregate(
            final_decision=final_decision,
            decisions=tuple(decisions),
            final_payload=final_payload,
            blocked_by=blocked_by,
        )

    async def _dispatch_one(
        self,
        spec: HookSpec,
        context: HookContext,
    ) -> HookDecision:
        """Dispatch a single hook by transport.

        Phase 4.0 Step 2: builtin + subprocess. HTTP / LLM deferred.
        """
        timeout = spec.timeout_ms or self._default_timeout_ms
        if spec.transport == "builtin":
            return await _invoke_builtin(spec, context, timeout_ms=timeout)
        if spec.transport == "subprocess":
            from harness.hooks.subprocess import invoke_subprocess_hook

            return await invoke_subprocess_hook(
                spec.script_path, context, timeout_ms=timeout
            )
        # HTTP / LLM deferred to Step 3.
        return HookDecision(
            decision="allow",
            hook_id=spec.hook_id,
            error=f"transport {spec.transport!r} not implemented yet",
        )


__all__ = ["HookRunner", "_invoke_builtin"]
