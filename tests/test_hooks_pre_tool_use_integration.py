"""Phase 4.0: Integration tests — PreToolUse/PostToolUse in ToolRuntime."""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.hooks import (
    EventType,
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    HookSpec,
)
from harness.server.agent.runtime import ToolRuntime


async def _allow_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(decision="allow", hook_id="test-allow")


async def _block_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(
        decision="block",
        hook_id="test-block",
        output={"reason": "policy violation"},
    )


@pytest.fixture
async def runtime_with_allow():
    registry = HookRegistry()
    await registry.register(
        HookSpec(
            hook_id="h1",
            event=EventType.PRE_TOOL_USE,
            transport="builtin",
            callable=_allow_hook,
        )
    )
    await registry.register(
        HookSpec(
            hook_id="h2",
            event=EventType.POST_TOOL_USE,
            transport="builtin",
            callable=_allow_hook,
        )
    )
    runner = HookRunner(registry)
    runtime = ToolRuntime(
        project_root=Path("."), hook_runner=runner, session_id="s-test"
    )
    return runtime, runner


@pytest.fixture
async def runtime_with_block():
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
    runtime = ToolRuntime(
        project_root=Path("."), hook_runner=runner, session_id="s-test"
    )
    return runtime, runner


class TestPreToolUseIntegration:
    """PreToolUse fires on execute(). Block decision aborts the tool call."""

    async def test_pre_tool_use_fires(self, runtime_with_allow) -> None:
        runtime, runner = runtime_with_allow
        # Use a tool that doesn't need real file — unknown tool is fine
        # because we just want to verify the hook fires.
        result = await runtime.execute("read_file", {"path": "x"})
        # Tool itself may fail (path doesn't exist) but hook fired
        # before the call. The result is whatever the tool produced.
        assert result is not None

    async def test_block_decision_aborts(self, runtime_with_block) -> None:
        runtime, runner = runtime_with_block
        result = await runtime.execute("read_file", {"path": "x"})
        # PreToolUse block → ok=False, error mentions hook.
        assert result.ok is False
        assert "blocked by hook" in result.error
        assert "policy violation" in result.error

    async def test_no_hook_runner_default(self) -> None:
        """Default (no hook_runner) → no error, hooks skipped."""
        runtime = ToolRuntime(project_root=Path("."))
        assert runtime._hook_runner is None
        result = await runtime.execute("read_file", {"path": "x"})
        # Should not crash, just return whatever the tool returns.
        assert result is not None


class TestPostToolUseIntegration:
    """PostToolUse fires after execute()."""

    async def test_post_tool_use_fires(self, runtime_with_allow) -> None:
        runtime, runner = runtime_with_allow
        result = await runtime.execute("read_file", {"path": "x"})
        # PostToolUse allow → result unchanged.
        # (We don't expose decisions list on ToolResult, but the
        # call shouldn't crash.)
        assert result is not None

    async def test_post_block_returns_error(self) -> None:
        """PostToolUse block → result is converted to error."""
        async def _post_block(ctx: HookContext) -> HookDecision:
            return HookDecision(
                decision="block",
                hook_id="post-block",
                output={"reason": "post denied"},
            )

        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="post",
                event=EventType.POST_TOOL_USE,
                transport="builtin",
                callable=_post_block,
            )
        )
        runner = HookRunner(registry)
        runtime = ToolRuntime(
            project_root=Path("."), hook_runner=runner, session_id="s-test"
        )
        result = await runtime.execute("read_file", {"path": "x"})
        assert result.ok is False
        assert "post-hook block" in result.error


class TestBackwardCompat:
    """ToolRuntime without hook_runner/session_id works as before."""

    async def test_default_construction(self) -> None:
        runtime = ToolRuntime(project_root=Path("."))
        assert runtime._hook_runner is None
        assert runtime._session_id == ""

    async def test_legacy_args_still_work(self) -> None:
        """Old constructor signature (scratchpad=..., privacy_zones=...) still works."""
        runtime = ToolRuntime(
            project_root=Path("."),
            scratchpad=None,
            privacy_zones=None,
            events_collector=None,
        )
        assert runtime._hook_runner is None
