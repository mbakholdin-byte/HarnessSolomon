"""Phase 4.0: Tests for HookRunner dispatch + timeout + aggregation."""
from __future__ import annotations

import asyncio

import pytest

from harness.hooks import (
    EventType,
    HookAggregate,
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    HookSpec,
)


async def _allow_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(decision="allow", hook_id="allow", output={})


async def _block_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(
        decision="block", hook_id="block", output={"reason": "denied"}
    )


async def _modify_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(
        decision="modify",
        hook_id="modify",
        output={"payload": {"modified": True}},
    )


async def _slow_hook(ctx: HookContext) -> HookDecision:
    await asyncio.sleep(0.5)
    return HookDecision(decision="allow", hook_id="slow")


async def _error_hook(ctx: HookContext) -> HookDecision:
    raise RuntimeError("oops")


class TestHookRunnerBasics:
    """HookRunner basic dispatch and aggregation."""

    async def test_no_hooks_returns_allow(self) -> None:
        runner = HookRunner(HookRegistry())
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert agg.decisions == ()

    async def test_single_allow(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_allow_hook,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert len(agg.decisions) == 1

    async def test_single_block(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_block_hook,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "block"
        assert agg.blocked_by == "h1"

    async def test_single_modify(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_modify_hook,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={"k": "v"}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "modify"
        assert agg.final_payload == {"modified": True}


class TestHookRunnerAggregation:
    """Aggregation order: block > modify > allow."""

    async def test_block_wins(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_allow_hook,
                priority=10,
            )
        )
        await registry.register(
            HookSpec(
                hook_id="h2",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_block_hook,
                priority=20,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "block"
        assert agg.blocked_by == "h2"

    async def test_modify_overrides_allow(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_allow_hook,
                priority=10,
            )
        )
        await registry.register(
            HookSpec(
                hook_id="h2",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_modify_hook,
                priority=20,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "modify"
        assert agg.final_payload == {"modified": True}

    async def test_all_allow(self) -> None:
        registry = HookRegistry()
        for i in range(3):
            await registry.register(
                HookSpec(
                    hook_id=f"h{i}",
                    event=EventType.PRE_TOOL_USE,
                    transport="builtin",
                    callable=_allow_hook,
                )
            )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert len(agg.decisions) == 3


class TestHookRunnerTimeout:
    """Timeout enforced via asyncio.wait_for."""

    async def test_timeout_fails_open(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="slow",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_slow_hook,
                timeout_ms=50,
            )
        )
        runner = HookRunner(registry, default_timeout_ms=50)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        # Fail-open: timeout allows the operation.
        assert agg.final_decision == "allow"
        assert "timeout" in agg.decisions[0].error

    async def test_exception_fails_open_by_default(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="error",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_error_hook,
            )
        )
        runner = HookRunner(registry, fail_open=True)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert "RuntimeError" in agg.decisions[0].error

    async def test_exception_fails_closed(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="error",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_error_hook,
            )
        )
        runner = HookRunner(registry, fail_open=False)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "block"
        assert agg.blocked_by == "error"


class TestHookRunnerRecursion:
    """Recursion + reentrancy guards prevent infinite loops."""

    async def test_recursion_depth_limit(self) -> None:
        runner = HookRunner(HookRegistry(), max_recursion_depth=2)
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={},
            recursion_depth=3,
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert agg.decisions == ()

    async def test_reentrancy_guard(self) -> None:
        runner = HookRunner(HookRegistry(), max_recursion_depth=10)
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={},
            event_stack=("PreToolUse",),
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert agg.decisions == ()


class TestHookRunnerFilter:
    """Per-spec matcher + global filter skip non-matching hooks."""

    async def test_spec_matcher_skips(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_block_hook,
                matcher="tool_name=write_*",
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "read_file"},
        )
        agg = await runner.fire(ctx)
        # Filter mismatch → no hook fires → allow.
        assert agg.final_decision == "allow"
        assert agg.decisions == ()

    async def test_global_filter_skips(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_block_hook,
            )
        )
        runner = HookRunner(registry, global_filter="tool_name=!rm")
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "rm"},
        )
        agg = await runner.fire(ctx)
        # Global filter: tool_name=rm does NOT match !rm → skip.
        assert agg.final_decision == "allow"

    async def test_max_per_event_cap(self) -> None:
        registry = HookRegistry()
        for i in range(5):
            await registry.register(
                HookSpec(
                    hook_id=f"h{i}",
                    event=EventType.PRE_TOOL_USE,
                    transport="builtin",
                    callable=_block_hook,
                )
            )
        runner = HookRunner(registry, max_per_event=2)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        # Only 2 hooks fired.
        assert len(agg.decisions) == 2


class TestHookRunnerDispatch:
    """Non-builtin transports return placeholder in Step 1."""

    async def test_subprocess_returns_placeholder(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="subprocess",
                script_path="/tmp/hook.py",
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert "not implemented" in agg.decisions[0].error
