"""Phase 4.5 v1.15.0: tests for ``harness hooks dispatch`` CLI subcommand.

Covers:
  - ``harness hooks dispatch <event>`` fires the event and prints
    the aggregate decision.
  - Invalid event name → exit code 2 with an error message.

Strategy: invoke the subcommand handler directly (no subprocess) to
keep tests fast. We wire a fresh ``HookRunner`` into the global
handle so the dispatch observes a known hook.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterator

import pytest

from harness.cli_hooks import _cmd_hooks_dispatch
from harness.hooks.context import HookContext, HookDecision
from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec, reset_registry
from harness.hooks.runner import HookRunner, set_global_hook_runner


# === Shared fixtures ====================================================


@pytest.fixture
def fresh_runner() -> Iterator[HookRunner]:
    """Bind a clean HookRunner to the global handle for the test."""
    registry = HookRegistry()
    runner = HookRunner(registry, default_timeout_ms=500)
    set_global_hook_runner(runner)
    yield runner
    set_global_hook_runner(None)
    reset_registry()


@pytest.fixture(autouse=True)
def _reset_global_runner() -> Iterator[None]:
    """Ensure no leftover global runner leaks between tests."""
    set_global_hook_runner(None)
    reset_registry()
    yield
    set_global_hook_runner(None)
    reset_registry()


def _ns(
    *,
    event: str = "",
    session: str = "",
    agent: str = "",
    payload: str = "{}",
    project_root: str | None = None,
    json_output: bool = False,
) -> argparse.Namespace:
    """Build a minimal argparse.Namespace mirroring the dispatch parser."""
    return argparse.Namespace(
        event=event,
        session=session,
        agent=agent,
        payload=payload,
        project_root=project_root,
        json=json_output,
    )


# === dispatch: fires event =============================================


def test_cli_dispatch_fires_event(
    fresh_runner: HookRunner,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """``harness hooks dispatch`` fires the event and prints the decision.

    We register a ``block`` hook on ``OnRoutingDecision`` and verify
    the CLI prints ``decision: block``.
    """
    import asyncio

    async def _block_hook(ctx: HookContext) -> HookDecision:
        return HookDecision(decision="block", hook_id="test.dispatch.block")

    asyncio.run(
        fresh_runner._registry.register(  # noqa: SLF001
            HookSpec(
                hook_id="test.dispatch.block",
                event=EventType.ON_ROUTING_DECISION,
                transport="builtin",
                callable=_block_hook,
            )
        )
    )

    rc = _cmd_hooks_dispatch(
        _ns(
            event="OnRoutingDecision",
            agent="explore",
            payload=json.dumps({"chosen_agent": "explore"}),
            project_root=str(tmp_path),
        )
    )
    out, err = capsys.readouterr()
    assert rc == 0, f"unexpected exit code {rc}; stderr={err}"
    assert "decision : block" in out, (
        f"expected 'decision : block' in stdout, got: {out!r}"
    )
    assert "event    : OnRoutingDecision" in out


def test_cli_dispatch_validates_event_name(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """An invalid event name exits with code 2 and an error message."""
    rc = _cmd_hooks_dispatch(
        _ns(event="NotARealEvent", project_root=str(tmp_path)),
    )
    out, err = capsys.readouterr()
    assert rc == 2, f"expected exit code 2 for invalid event, got {rc}"
    assert "unknown event" in err.lower(), (
        f"expected 'unknown event' in stderr, got: {err!r}"
    )
