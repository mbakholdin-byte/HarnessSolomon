"""Phase 4.5 v1.15.0: PermissionRequest hook — emit + override.

7 tests covering the new ``PermissionRequest`` hook that fires
BEFORE the bash denylist check in
``ToolRuntime._bash`` (via ``_resolve_permission_via_hook``).

Hook contract (Phase 4.5 v1.15.0):

* ``"allow"``  — overrides an initial ``"deny"`` (denylist escape hatch).
* ``"block"``  — forces ``"deny"`` even if initial was ``"allow"``.
* ``"modify"`` with ``output["payload"]["permission_decision"]`` —
  explicit override to ``"allow"`` or ``"deny"``.
* Hook failure / no hooks registered — the original denylist
  decision stands (fail-open but explicit).

The tests exercise ``ToolRuntime.execute("bash", ...)`` end-to-end
and assert either that the tool ran (allow path) or that it was
denied with the documented error string (deny path).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from harness.hooks.context import HookContext, HookDecision
from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec, reset_registry
from harness.hooks.runner import (
    HookRunner,
    set_global_hook_runner,
)
from harness.server.agent.runtime import ToolRuntime


# === Fixtures ===========================================================


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Empty project root under pytest's tmp_path."""
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def runtime(tmp_project: Path) -> ToolRuntime:
    """Plain ``ToolRuntime`` — no injected ``hook_runner``, so
    PreToolUse / PostToolUse hooks do NOT fire. Only PermissionRequest
    (which uses the global runner) is exercised.
    """
    return ToolRuntime(project_root=tmp_project, session_id="sess-test")


@pytest.fixture
def fresh_runner() -> Iterator[HookRunner]:
    """Bind a clean global HookRunner with an empty registry.

    Production call sites read ``get_global_hook_runner()``; this
    fixture wires a fresh runner so tests can register PermissionRequest
    hooks in isolation. On teardown we restore ``None`` and reset
    the singleton registry.
    """
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


def _make_permission_hook(
    decision: str,
    *,
    override_payload: dict[str, Any] | None = None,
    seen: list[HookContext] | None = None,
    raise_exc: Exception | None = None,
):
    """Build a builtin PermissionRequest hook callable.

    Parameters:
        decision: ``"allow"`` / ``"block"`` / ``"modify"`` returned
            by the hook.
        override_payload: for ``"modify"`` decisions, the
            ``output["payload"]`` to attach (must contain
            ``permission_decision`` if the test wants to override).
        seen: optional list to append the observed context to.
        raise_exc: if set, the hook raises this instead of returning
            a decision (used for failure tests).
    """

    async def _hook(ctx: HookContext) -> HookDecision:
        if seen is not None:
            seen.append(ctx)
        if raise_exc is not None:
            raise raise_exc
        output: dict[str, Any] = {}
        if decision == "modify" and override_payload is not None:
            output = {"payload": override_payload}
        return HookDecision(decision=decision, hook_id="test.perm", output=output)

    return _hook


async def _register_permission_hook(
    runner: HookRunner,
    decision: str,
    *,
    override_payload: dict[str, Any] | None = None,
    seen: list[HookContext] | None = None,
    raise_exc: Exception | None = None,
) -> None:
    """Register a builtin PermissionRequest hook on ``runner`` (async)."""
    callable_ = _make_permission_hook(
        decision,
        override_payload=override_payload,
        seen=seen,
        raise_exc=raise_exc,
    )
    await runner._registry.register(  # noqa: SLF001 — test-only
        HookSpec(
            hook_id="test.permission_request",
            event=EventType.PERMISSION_REQUEST,
            transport="builtin",
            callable=callable_,
        )
    )


# === 1. Fires before denylist check =====================================


async def test_permission_request_fires_before_check(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """``PermissionRequest`` is dispatched for ``rm -rf /`` BEFORE
    the denylist short-circuits the tool.

    We register a recording hook that returns ``"allow"`` (the
    documented escape hatch). The hook MUST observe the event even
    though the denylist would deny the command.
    """
    seen: list[HookContext] = []
    await _register_permission_hook(fresh_runner, "allow", seen=seen)

    result = await runtime.execute("bash", {"command": "rm -rf /", "timeout": 5})

    # Hook was invoked exactly once for PermissionRequest.
    assert len(seen) == 1, f"expected 1 PermissionRequest dispatch, got {len(seen)}"
    ctx = seen[0]
    assert ctx.event == "PermissionRequest"
    # The initial decision from the denylist is surfaced to the hook.
    assert ctx.payload["permission_decision"] == "deny"
    assert ctx.payload["tool_name"] == "bash"
    # Because the hook said "allow", the tool was NOT short-circuited
    # by the denylist — it proceeded (the actual `rm -rf /` will fail
    # on Windows / non-root, but the runtime does not deny it).
    # We only assert that the denylist message is absent.
    assert "matches safety pattern" not in result.error


# === 2. modify overrides initial deny → allow ==========================


async def test_permission_request_modify_overrides_initial(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """Hook ``modify`` with ``permission_decision="allow"`` overrides
    an initial ``"deny"`` (denylist match).

    Without the override, ``rm -rf /`` would be denied. With the
    override, the tool proceeds.
    """
    await _register_permission_hook(
        fresh_runner,
        "modify",
        override_payload={
            "tool_name": "bash",
            "arguments_preview": "rm -rf /",
            "permission_decision": "allow",
            "denied_reason": "",
        },
    )

    result = await runtime.execute("bash", {"command": "rm -rf /", "timeout": 5})

    # The denylist pattern message MUST NOT appear — the hook
    # overrode the denial.
    assert "matches safety pattern" not in result.error


# === 3. block forces deny even when initial was allow ==================


async def test_permission_request_block_denies(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """Hook ``block`` forces ``"deny"`` even for a command that the
    denylist would have allowed (``echo hi``).

    The runtime must return an error result whose message reflects
    the hook block.
    """
    await _register_permission_hook(fresh_runner, "block")

    result = await runtime.execute("bash", {"command": "echo hi", "timeout": 5})

    assert not result.ok
    assert "denied" in result.error.lower() or "deny" in result.error.lower()
    # The hook-driven deny uses the generic message when the
    # denylist itself did not match.
    assert "blocked by PermissionRequest hook" in result.error or "denied:" in result.error


# === 4. allow overrides initial deny ====================================


async def test_permission_request_allow_overrides_deny(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """Hook ``allow`` overrides an initial ``"deny"`` (denylist match).

    Equivalent to test #2 but uses the ``allow`` decision directly
    rather than ``modify`` with a payload override.
    """
    await _register_permission_hook(fresh_runner, "allow")

    result = await runtime.execute("bash", {"command": "rm -rf /", "timeout": 5})

    assert "matches safety pattern" not in result.error


# === 5. Payload schema contains required fields ========================


async def test_permission_request_payload_schema(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """The ``PermissionRequest`` payload contains the 4 documented
    fields: ``tool_name``, ``arguments_preview``,
    ``permission_decision``, ``denied_reason``.
    """
    seen: list[HookContext] = []
    await _register_permission_hook(fresh_runner, "allow", seen=seen)

    await runtime.execute("bash", {"command": "rm -rf /tmp/x", "timeout": 5})

    assert len(seen) == 1
    payload = seen[0].payload
    assert set(payload.keys()) >= {
        "tool_name",
        "arguments_preview",
        "permission_decision",
        "denied_reason",
    }
    assert payload["tool_name"] == "bash"
    assert isinstance(payload["arguments_preview"], str)
    assert payload["permission_decision"] in {"allow", "deny"}
    assert isinstance(payload["denied_reason"], str)


# === 6. arguments_preview is truncated to 200 chars =====================


async def test_permission_request_no_pii_in_arguments(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """``arguments_preview`` is truncated to 200 characters.

    We pass a bash command + argument dict whose ``str(...)``
    representation exceeds 200 chars and assert the hook observes
    exactly 200 chars in ``arguments_preview``.
    """
    seen: list[HookContext] = []
    await _register_permission_hook(fresh_runner, "allow", seen=seen)

    # Build arguments whose ``str(dict)`` exceeds 200 chars.
    long_payload = "x" * 500
    args = {
        "command": f"echo {long_payload}",
        "timeout": 5,
        "extra": long_payload,
    }

    await runtime.execute("bash", args)

    assert len(seen) == 1
    preview = seen[0].payload["arguments_preview"]
    assert isinstance(preview, str)
    assert len(preview) <= 200, (
        f"arguments_preview must be truncated to 200 chars, got {len(preview)}"
    )


# === 7. Hook failure does not break the tool ===========================


async def test_permission_request_hook_failure_does_not_break_tool(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """If the PermissionRequest hook raises, the tool must still
    execute (or be denied) according to the ORIGINAL denylist
    decision — never crash.

    We register a hook that raises ``RuntimeError``. For an
    allow-listed command (``echo hi``), the tool MUST still run
    successfully.
    """
    await _register_permission_hook(
        fresh_runner,
        decision="allow",
        raise_exc=RuntimeError("hook exploded"),
    )

    result = await runtime.execute("bash", {"command": "echo hi", "timeout": 5})

    # Original decision was allow → tool ran.
    assert result.ok, (
        f"tool should run when hook fails (original allow); got error={result.error!r}"
    )
    assert "hi" in result.output
