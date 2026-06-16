"""Phase 4.0: Tests for HookRegistry + HookSpec + parse_spec."""
from __future__ import annotations

import pytest

from harness.hooks import EventType, HookRegistry
from harness.hooks.registry import (
    HookSpec,
    HookTransport,
    parse_spec,
)


async def _noop(ctx):  # type: ignore[no-untyped-def]
    """A noop async hook callable."""
    from harness.hooks import HookDecision

    return HookDecision(decision="allow", hook_id="noop")


class TestParseSpec:
    """parse_spec converts settings strings to HookSpec."""

    def test_builtin(self) -> None:
        spec = parse_spec("PreToolUse:builtin:log")
        assert spec.event is EventType.PRE_TOOL_USE
        assert spec.transport == "builtin"
        assert spec.hook_id == "user.builtin.log"

    def test_subprocess_with_timeout(self) -> None:
        spec = parse_spec("PreToolUse:subprocess:/path/to/hook.py:3000")
        assert spec.event is EventType.PRE_TOOL_USE
        assert spec.transport == "subprocess"
        assert spec.script_path == "/path/to/hook.py"
        assert spec.timeout_ms == 3000

    def test_subprocess_no_timeout(self) -> None:
        spec = parse_spec("PreToolUse:subprocess:/path/to/hook.py")
        assert spec.timeout_ms is None
        assert spec.script_path == "/path/to/hook.py"

    def test_http_with_auth(self) -> None:
        spec = parse_spec("OnMemoryWrite:http:https://example.com/hook:Bearer abc123:5000")
        assert spec.event is EventType.ON_MEMORY_WRITE
        assert spec.transport == "http"
        assert spec.url == "https://example.com/hook"
        assert spec.timeout_ms == 5000
        assert spec.headers == {"Authorization": "Bearer abc123"}

    def test_http_no_auth(self) -> None:
        spec = parse_spec("OnMemoryWrite:http:https://example.com/hook:5000")
        assert spec.url == "https://example.com/hook"
        assert spec.headers == {}

    def test_llm(self) -> None:
        spec = parse_spec(
            "OnRoutingDecision:llm:qwen3-8b:3000:Decide whether to override"
        )
        assert spec.event is EventType.ON_ROUTING_DECISION
        assert spec.transport == "llm"
        assert spec.model == "qwen3-8b"
        assert spec.timeout_ms == 3000
        assert spec.prompt == "Decide whether to override"

    def test_invalid_event(self) -> None:
        with pytest.raises(ValueError, match="is not a valid EventType|NotReal"):
            parse_spec("NotReal:builtin:log")

    def test_invalid_transport(self) -> None:
        with pytest.raises(ValueError, match="Invalid hook spec"):
            parse_spec("PreToolUse:unknown:foo")

    def test_invalid_builtin_too_many_args(self) -> None:
        with pytest.raises(ValueError, match="Invalid builtin spec"):
            parse_spec("PreToolUse:builtin:log:extra")

    def test_invalid_subprocess_too_many_args(self) -> None:
        with pytest.raises(ValueError, match="Invalid subprocess spec"):
            parse_spec("PreToolUse:subprocess:/a.py:3000:extra")

    def test_invalid_http_no_url(self) -> None:
        with pytest.raises(ValueError, match="Invalid hook spec|://"):
            parse_spec("PreToolUse:http:")

    def test_invalid_llm_no_prompt(self) -> None:
        with pytest.raises(ValueError, match="Invalid llm spec|Missing prompt"):
            parse_spec("PreToolUse:llm:qwen3-8b")


class TestHookSpec:
    """HookSpec is a frozen dataclass with typed fields."""

    def test_minimal(self) -> None:
        spec = HookSpec(
            hook_id="x",
            event=EventType.PRE_TOOL_USE,
            transport="builtin",
        )
        assert spec.enabled is True
        assert spec.priority == 100
        assert spec.timeout_ms is None
        assert spec.matcher == ""

    def test_frozen(self) -> None:
        spec = HookSpec(
            hook_id="x",
            event=EventType.PRE_TOOL_USE,
            transport="builtin",
        )
        with pytest.raises(Exception):
            spec.hook_id = "y"  # type: ignore[misc]

    def test_with_callable(self) -> None:
        spec = HookSpec(
            hook_id="builtin.log",
            event=EventType.PRE_TOOL_USE,
            transport="builtin",
            callable=_noop,
        )
        assert spec.callable is _noop


class TestHookRegistry:
    """HookRegistry is the in-memory event → [hooks] mapping."""

    async def test_empty(self) -> None:
        r = HookRegistry()
        assert len(r) == 0
        assert r.for_event(EventType.PRE_TOOL_USE) == []

    async def test_register(self) -> None:
        r = HookRegistry()
        spec = HookSpec(
            hook_id="h1",
            event=EventType.PRE_TOOL_USE,
            transport="builtin",
        )
        await r.register(spec)
        assert len(r) == 1
        assert r.for_event(EventType.PRE_TOOL_USE) == [spec]

    async def test_register_replaces_same_id(self) -> None:
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                priority=100,
            )
        )
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                priority=50,
            )
        )
        specs = r.for_event(EventType.PRE_TOOL_USE)
        assert len(specs) == 1
        assert specs[0].priority == 50

    async def test_register_sorts_by_priority(self) -> None:
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                priority=200,
            )
        )
        await r.register(
            HookSpec(
                hook_id="h2",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                priority=50,
            )
        )
        specs = r.for_event(EventType.PRE_TOOL_USE)
        assert [s.hook_id for s in specs] == ["h2", "h1"]

    async def test_unregister_existing(self) -> None:
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
            )
        )
        assert await r.unregister("h1") is True
        assert r.for_event(EventType.PRE_TOOL_USE) == []

    async def test_unregister_missing(self) -> None:
        r = HookRegistry()
        assert await r.unregister("nonexistent") is False

    async def test_set_enabled(self) -> None:
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
            )
        )
        assert await r.set_enabled("h1", False) is True
        specs = r.for_event(EventType.PRE_TOOL_USE)
        assert specs[0].enabled is False

    async def test_set_enabled_missing(self) -> None:
        r = HookRegistry()
        assert await r.set_enabled("nonexistent", False) is False

    async def test_for_event_snapshot(self) -> None:
        """for_event returns a list (not a view), safe to mutate."""
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
            )
        )
        snapshot = r.for_event(EventType.PRE_TOOL_USE)
        snapshot.clear()
        # Original is unchanged.
        assert len(r.for_event(EventType.PRE_TOOL_USE)) == 1

    async def test_all_specs(self) -> None:
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
            )
        )
        await r.register(
            HookSpec(
                hook_id="h2",
                event=EventType.POST_TOOL_USE,
                transport="builtin",
            )
        )
        all_s = r.all_specs()
        assert len(all_s) == 2
        assert {s.hook_id for s in all_s} == {"h1", "h2"}

    async def test_contains(self) -> None:
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
            )
        )
        assert "h1" in r
        assert "h2" not in r

    async def test_registration_across_events(self) -> None:
        """Hooks for different events are stored independently."""
        r = HookRegistry()
        await r.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
            )
        )
        await r.register(
            HookSpec(
                hook_id="h2",
                event=EventType.POST_TOOL_USE,
                transport="builtin",
            )
        )
        assert len(r.for_event(EventType.PRE_TOOL_USE)) == 1
        assert len(r.for_event(EventType.POST_TOOL_USE)) == 1
        assert len(r.for_event(EventType.STOP)) == 0
