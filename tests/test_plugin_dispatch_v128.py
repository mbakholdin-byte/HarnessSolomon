"""Phase 6.3 v1.28.0 — PluginDispatcher integration tests.

Covers:

* :class:`PluginDispatcher` dispatch semantics (order, isolation,
  disabled flag, empty event, exceptions).
* Integration with :class:`HookRunner`: firing a real event invokes
  plugin callbacks registered via :class:`PluginRegistry`.
* Backward compatibility: with no plugins loaded, the runner behaves
  exactly as in Phase 6.1 (no callbacks, no overhead beyond one
  ``None`` check).
* AST trust boundary on ``harness/plugins/dispatcher.py``.
* Shipped example plugins register their hooks cleanly.

Run::

    pytest tests/test_plugin_dispatch_v128.py -v
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from harness.hooks import (
    EventType,
    HookContext,
    HookRegistry,
    HookRunner,
    HookSpec,
    HookDecision,
)
from harness.hooks.runner import set_global_hook_runner, safe_fire
from harness.plugins import (
    PluginRegistry,
    get_registry,
    reset_registry,
)
from harness.plugins.dispatcher import PluginDispatcher
from harness.plugins.loader import load_plugins_from_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_plugin_registry() -> PluginRegistry:
    """Reset the global PluginRegistry before + after each test."""
    reset_registry()
    yield get_registry()
    reset_registry()


@pytest.fixture
def fresh_hook_registry() -> HookRegistry:
    """A clean HookRegistry (no builtin hooks loaded)."""
    return HookRegistry()


@pytest.fixture(autouse=True)
def _isolate_global_runner() -> Any:
    """Ensure no global HookRunner leaks between tests."""
    set_global_hook_runner(None)
    yield
    set_global_hook_runner(None)


def _allow_hook(ctx: HookContext) -> HookDecision:
    """Trivial allow builtin hook (for HookRunner integration tests)."""
    return HookDecision(decision="allow", hook_id="builtin_allow")


# ---------------------------------------------------------------------------
# 1. PluginDispatcher — basic dispatch semantics
# ---------------------------------------------------------------------------

async def test_plugin_hook_fires_on_real_event(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
) -> None:
    """A real HookRunner.fire() invokes the plugin callback."""
    seen: list[dict[str, Any]] = []

    def plugin_cb(event: dict[str, Any]) -> None:
        seen.append(event)

    fresh_plugin_registry.register_hook(
        "PreToolUse", plugin_cb, plugin_name="test_logger",
    )
    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    dispatcher.subscribe_all()

    runner = HookRunner(fresh_hook_registry, plugin_dispatcher=dispatcher)
    ctx = HookContext(
        event="PreToolUse",
        session_id="s1",
        agent_id="",
        payload={"tool_name": "read_file", "arguments": {}},
    )
    await runner.fire(ctx)

    assert len(seen) == 1
    assert seen[0]["tool_name"] == "read_file"


async def test_plugin_hook_not_fires_when_disabled(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
) -> None:
    """When dispatcher.enabled=False, plugin callbacks are NOT invoked."""
    seen: list[dict[str, Any]] = []

    fresh_plugin_registry.register_hook(
        "PreToolUse", lambda e: seen.append(e), plugin_name="p",
    )
    dispatcher = PluginDispatcher(
        fresh_plugin_registry, runner=None, enabled=False,
    )

    runner = HookRunner(fresh_hook_registry, plugin_dispatcher=dispatcher)
    ctx = HookContext(
        event="PreToolUse", session_id="s1", agent_id="", payload={},
    )
    await runner.fire(ctx)

    assert seen == []


async def test_multiple_plugins_same_event_all_called(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """Two plugins on the same event are both called in registration order."""
    order: list[str] = []

    fresh_plugin_registry.register_hook(
        "OnToolUse", lambda e: order.append("first"), plugin_name="p1",
    )
    fresh_plugin_registry.register_hook(
        "OnToolUse", lambda e: order.append("second"), plugin_name="p2",
    )

    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    results = await dispatcher.dispatch("OnToolUse", {"tool_name": "x"})

    assert order == ["first", "second"]
    # Both callbacks returned None (lambda returning append's return).
    assert len(results) == 2


async def test_plugin_callback_exception_logged_not_crash(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin callback that raises is logged; the rest still run."""
    survivors: list[str] = []

    def boom(_: dict[str, Any]) -> None:
        raise RuntimeError("plugin exploded")

    def ok(event: dict[str, Any]) -> None:
        survivors.append(event.get("tool_name", "?"))

    fresh_plugin_registry.register_hook("OnToolUse", boom, plugin_name="bad")
    fresh_plugin_registry.register_hook("OnToolUse", ok, plugin_name="good")

    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)

    with caplog.at_level("WARNING", logger="harness.plugins.dispatcher"):
        results = await dispatcher.dispatch("OnToolUse", {"tool_name": "t"})

    # The good callback ran despite the bad one raising.
    assert survivors == ["t"]
    # The bad callback's failure was logged.
    assert any("plugin_dispatch" in r.message for r in caplog.records)
    # Results list still has 2 entries (None for the failed one).
    assert len(results) == 2
    assert results[0] is None  # boom failed


async def test_plugin_hook_payload_matches_runner_contract(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
) -> None:
    """Payload dict shape received by plugins matches HookRunner's payload."""
    received: dict[str, Any] = {}

    def capture(event: dict[str, Any]) -> None:
        received.update(event)

    fresh_plugin_registry.register_hook(
        "PostToolUse", capture, plugin_name="cap",
    )
    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    runner = HookRunner(fresh_hook_registry, plugin_dispatcher=dispatcher)

    payload = {
        "tool_name": "write_file",
        "arguments": {},
        "ok": True,
        "output": "ok",
        "session_id": "s9",
    }
    ctx = HookContext(
        event="PostToolUse", session_id="s9", agent_id="a1", payload=payload,
    )
    await runner.fire(ctx)

    # Plugin receives the schema-validated payload (extra fields like
    # session_id are dropped by ConfigDict(extra="ignore"); the schema-
    # defined fields are present).
    assert received["tool_name"] == "write_file"
    assert received["ok"] is True
    assert received["output"] == "ok"


async def test_backward_compat_no_plugins_same_behavior(
    fresh_hook_registry: HookRegistry,
) -> None:
    """With no plugins + no dispatcher, HookRunner behaves as Phase 6.1."""
    runner = HookRunner(fresh_hook_registry)  # plugin_dispatcher=None
    ctx = HookContext(
        event="PreToolUse", session_id="s1", agent_id="", payload={},
    )
    agg = await runner.fire(ctx)
    assert agg.final_decision == "allow"
    assert agg.decisions == ()


async def test_end_to_end_plugin_logs_on_real_tool_call(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
) -> None:
    """A simulated tool call fires PostToolUse → plugin records it."""
    log: list[str] = []

    def tool_logger(event: dict[str, Any]) -> None:
        log.append(f"{event['tool_name']}:{event.get('output', '')}")

    fresh_plugin_registry.register_hook(
        "PostToolUse", tool_logger, plugin_name="e2e_logger",
    )
    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    runner = HookRunner(fresh_hook_registry, plugin_dispatcher=dispatcher)
    set_global_hook_runner(runner)
    try:
        # Simulate what ToolRuntime.execute does after a tool runs.
        await safe_fire(
            "PostToolUse",
            payload={
                "tool_name": "read_file",
                "arguments": {},
                "ok": True,
                "output": "42 bytes",
            },
            session_id="s1",
        )
    finally:
        set_global_hook_runner(None)

    assert log == ["read_file:42 bytes"]


def test_subscribe_all_registers_all_known_events(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """subscribe_all() tracks every EventType member."""
    from harness.hooks.events import EventType

    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    assert dispatcher.subscribed_events == frozenset()

    dispatcher.subscribe_all()

    expected = {e.value for e in EventType}
    assert dispatcher.subscribed_events == expected


async def test_dispatcher_respects_dispatch_enabled_setting(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """Toggling set_enabled(False) at runtime silences dispatch."""
    calls: list[dict[str, Any]] = []
    fresh_plugin_registry.register_hook(
        "OnToolUse", lambda e: calls.append(e), plugin_name="p",
    )
    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    assert dispatcher.enabled is True

    await dispatcher.dispatch("OnToolUse", {"tool_name": "a"})
    assert len(calls) == 1

    dispatcher.set_enabled(False)
    await dispatcher.dispatch("OnToolUse", {"tool_name": "b"})
    assert len(calls) == 1  # no new call

    dispatcher.set_enabled(True)
    await dispatcher.dispatch("OnToolUse", {"tool_name": "c"})
    assert len(calls) == 2


async def test_plugin_hook_result_collected(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """Non-None return values are collected in the results list."""
    def add_one(event: dict[str, Any]) -> int:
        return event["x"] + 1

    def shout(event: dict[str, Any]) -> str:
        return event["x"] * 10

    fresh_plugin_registry.register_hook("OnToolUse", add_one, plugin_name="p1")
    fresh_plugin_registry.register_hook("OnToolUse", shout, plugin_name="p2")

    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    results = await dispatcher.dispatch("OnToolUse", {"x": 5})

    assert results == [6, 50]


async def test_empty_event_type_no_callbacks_invoked(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """dispatch("") returns [] without touching the registry."""
    called: list[bool] = []
    fresh_plugin_registry.register_hook(
        "OnToolUse", lambda e: called.append(True), plugin_name="p",
    )
    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)

    results = await dispatcher.dispatch("", {"tool_name": "x"})
    assert results == []
    assert called == []


async def test_unregister_hook_not_called(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """A hook name with no registered callbacks produces empty results.

    This covers the case where a plugin was loaded but never registered
    a handler for the dispatched event name — the registry returns []
    and the dispatcher returns [] without invoking anything.
    """
    # Register on one event, dispatch a DIFFERENT event.
    fresh_plugin_registry.register_hook(
        "OnToolUse", lambda e: None, plugin_name="p",
    )
    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    results = await dispatcher.dispatch("OnCompaction", {})
    assert results == []


# ---------------------------------------------------------------------------
# 2. HookRunner — DI wiring + dispatcher injection
# ---------------------------------------------------------------------------

async def test_plugin_dispatcher_initialization_order(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
) -> None:
    """Runner can be constructed WITHOUT a dispatcher and wired later.

    This mirrors the lifespan sequence:
      1. HookRunner constructed (plugin_dispatcher=None by default).
      2. PluginDispatcher constructed with registry + runner.
      3. runner.set_plugin_dispatcher(dispatcher) wires it in.
    """
    runner = HookRunner(fresh_hook_registry)
    assert runner.plugin_dispatcher is None

    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=runner)
    runner.set_plugin_dispatcher(dispatcher)

    assert runner.plugin_dispatcher is dispatcher
    assert dispatcher.runner is runner


async def test_runner_without_dispatcher_skips_dispatch(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
) -> None:
    """A runner with plugin_dispatcher=None never invokes plugin callbacks."""
    invoked: list[dict[str, Any]] = []
    fresh_plugin_registry.register_hook(
        "PreToolUse", lambda e: invoked.append(e), plugin_name="p",
    )
    # Note: registry has plugins, but runner has NO dispatcher wired.
    runner = HookRunner(fresh_hook_registry)
    ctx = HookContext(
        event="PreToolUse", session_id="s1", agent_id="", payload={},
    )
    await runner.fire(ctx)
    assert invoked == []


# ---------------------------------------------------------------------------
# 3. Shipped example plugins
# ---------------------------------------------------------------------------

def test_tool_logger_example_plugin_registers_successfully(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """The shipped .harness/plugins/tool_logger.py loads + registers OnToolUse."""
    repo_root = Path(__file__).resolve().parent.parent
    plugin_path = repo_root / ".harness" / "plugins" / "tool_logger.py"
    assert plugin_path.is_file(), f"missing plugin at {plugin_path}"

    loaded = load_plugins_from_dir(
        plugin_path.parent,
        registry=fresh_plugin_registry,
        allowed=["tool_logger"],
    )
    assert len(loaded) == 1
    info = loaded[0]
    assert info.name == "tool_logger"
    assert "OnToolUse" in info.hooks

    handlers = fresh_plugin_registry.hooks_for("OnToolUse")
    assert len(handlers) == 1
    # The handler returns a structured record.
    result = handlers[0]({"tool_name": "read_file", "session_id": "s"})
    assert isinstance(result, dict)
    assert result["tool"] == "read_file"


def test_example_logger_still_registers_on_tool_use(
    fresh_plugin_registry: PluginRegistry,
) -> None:
    """The updated example_logger.py still loads + registers OnToolUse."""
    repo_root = Path(__file__).resolve().parent.parent
    plugin_path = repo_root / ".harness" / "plugins" / "example_logger.py"
    assert plugin_path.is_file()

    loaded = load_plugins_from_dir(
        plugin_path.parent,
        registry=fresh_plugin_registry,
        allowed=["example_logger"],
    )
    assert len(loaded) == 1
    assert "OnToolUse" in loaded[0].hooks


# ---------------------------------------------------------------------------
# 4. AST trust boundary
# ---------------------------------------------------------------------------

def test_plugin_dispatch_ast_trust_boundary() -> None:
    """dispatcher.py must NOT import harness.agents or harness.server.

    Static defence in depth on top of the runtime AST scan in the
    loader. Catches accidental imports introduced during refactoring.
    """
    repo_root = Path(__file__).resolve().parent.parent
    dispatcher_path = repo_root / "harness" / "plugins" / "dispatcher.py"
    assert dispatcher_path.is_file(), f"missing {dispatcher_path}"

    source = dispatcher_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(dispatcher_path))

    forbidden_prefixes = ("harness.agents", "harness.server")
    failures: list[str] = []
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append(node.module)
        for tgt in targets:
            if any(tgt == p or tgt.startswith(p + ".") for p in forbidden_prefixes):
                failures.append(tgt)

    assert not failures, (
        f"dispatcher.py must not import harness.agents / harness.server — "
        f"found: {failures}"
    )


# ---------------------------------------------------------------------------
# 5. Settings — plugins_dispatch_enabled
# ---------------------------------------------------------------------------

def test_plugins_dispatch_enabled_default_true() -> None:
    """The new setting defaults to True (opt-out)."""
    from harness.config import Settings
    s = Settings()
    assert s.plugins_dispatch_enabled is True


# ---------------------------------------------------------------------------
# 6. Payload isolation — plugin cannot mutate caller's dict
# ---------------------------------------------------------------------------

async def test_plugin_cannot_mutate_caller_payload(
    fresh_plugin_registry: PluginRegistry,
    fresh_hook_registry: HookRegistry,
) -> None:
    """Plugins receive a shallow copy; mutating it doesn't affect the runner."""
    def malicious(event: dict[str, Any]) -> None:
        event["tool_name"] = "PWNED"

    fresh_plugin_registry.register_hook(
        "PreToolUse", malicious, plugin_name="evil",
    )
    dispatcher = PluginDispatcher(fresh_plugin_registry, runner=None)
    runner = HookRunner(fresh_hook_registry, plugin_dispatcher=dispatcher)

    original = {"tool_name": "read_file", "arguments": {"path": "/tmp"}}
    ctx = HookContext(
        event="PreToolUse", session_id="s1", agent_id="", payload=original,
    )
    await runner.fire(ctx)

    # The caller's dict is untouched.
    assert original == {"tool_name": "read_file", "arguments": {"path": "/tmp"}}
