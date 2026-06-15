"""Tests for AgentRunner offloader factory (Phase 3 v1.3.1, Step 4)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.agents.runner import AgentRunner


# === Test doubles ===

class FakeScratchpad:
    """Minimal in-memory scratchpad double.

    The runner + ToolRuntime only need ``init()`` and ``_session_id``
    for the offload path. ``read_notes`` is provided so the runner's
    L0 read doesn't raise (it isn't asserted in these tests).
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.init_calls = 0
        self._session_id = session_id or "fake-sess"

    async def init(self) -> None:
        self.init_calls += 1

    async def read_notes(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class FakeRouter:
    """Stub router — runner never invokes it in these tests."""

    pass


# === Helpers ===

def _make_runner(
    tmp_path: Path,
    *,
    scratchpad_factory: Any = None,
    offloader_factory: Any = None,
) -> AgentRunner:
    return AgentRunner(
        router=FakeRouter(),  # type: ignore[arg-type]
        repo=tmp_path,
        scratchpad_factory=scratchpad_factory,
        offloader_factory=offloader_factory,
    )


@pytest.fixture
def spec() -> Any:
    s = MagicMock()
    s.name = "test-agent"
    s.worktree_required = False
    s.memory_namespace = None
    s.model = "qwen3:8b"
    s.max_iterations = 1
    s.permissions = "full"
    s.prompt = "do the thing"
    s.tool_overrides = None
    s.tool_append = None
    return s


@pytest.fixture
def wt() -> Any:
    """Minimal WorktreeInfo double."""
    w = MagicMock()
    w.path = Path("C:/tmp/fake_wt")
    w.branch = "h/test-agent/x"
    w.worktree_id = "wt-fake"
    return w


# === Factory wiring ===

class TestRunnerOffloadFactory:
    async def test_offloader_wired_when_factory_provided(
        self, tmp_path: Path, spec: Any, wt: Any,
    ) -> None:
        captured: dict[str, Any] = {}
        fake_offloader = MagicMock()
        fake_offloader._scratchpad = None
        fake_offloader._settings = MagicMock(tool_offload_max_ms=2000)

        def factory(*, spec, session_id, scratchpad):
            captured["spec"] = spec
            captured["session_id"] = session_id
            captured["scratchpad"] = scratchpad
            return fake_offloader

        scratchpad = FakeScratchpad(session_id="sess-X")
        runner = _make_runner(
            tmp_path,
            scratchpad_factory=lambda spec, sid: scratchpad,
            offloader_factory=factory,
        )
        await runner._drive(
            spec=spec, prompt="do the thing", wt=wt, stream=False,
            session_id="sess-X",
        )
        # The factory was invoked with the live spec + session + scratchpad.
        assert captured["spec"] is spec
        assert captured["session_id"] == "sess-X"
        assert captured["scratchpad"] is scratchpad

    async def test_offloader_none_when_setting_disabled(
        self, tmp_path: Path, spec: Any, wt: Any,
    ) -> None:
        """``offloader_factory=None`` → no offloader passed to the
        ToolRuntime (backward compat)."""
        from harness.server.agent.runtime import ToolRuntime

        captured: dict[str, Any] = {}
        real_TR = ToolRuntime

        class SpyToolRuntime(real_TR):
            def __init__(self, *a: Any, **kw: Any) -> None:
                captured["kw"] = kw
                super().__init__(*a, **kw)

        # Patch the ToolRuntime symbol the runner module imported.
        import harness.agents.runner as runner_mod
        original = runner_mod.ToolRuntime
        runner_mod.ToolRuntime = SpyToolRuntime  # type: ignore[assignment]
        try:
            scratchpad = FakeScratchpad(session_id="sess-Y")
            runner = _make_runner(
                tmp_path,
                scratchpad_factory=lambda spec, sid: scratchpad,
                offloader_factory=None,
            )
            await runner._drive(
                spec=spec, prompt="do the thing", wt=wt, stream=False,
                session_id="sess-Y",
            )
            # No tool_offloader kwarg passed (or it's None).
            assert captured["kw"].get("tool_offloader") is None
        finally:
            runner_mod.ToolRuntime = original

    async def test_factory_exception_fails_open(
        self, tmp_path: Path, spec: Any, wt: Any,
    ) -> None:
        """A factory that raises must NOT break the chat loop."""
        from harness.server.agent.runtime import ToolRuntime

        captured: dict[str, Any] = {}
        real_TR = ToolRuntime

        class SpyToolRuntime(real_TR):
            def __init__(self, *a: Any, **kw: Any) -> None:
                captured["kw"] = kw
                super().__init__(*a, **kw)

        import harness.agents.runner as runner_mod
        original = runner_mod.ToolRuntime
        runner_mod.ToolRuntime = SpyToolRuntime  # type: ignore[assignment]
        try:
            scratchpad = FakeScratchpad(session_id="sess-Z")

            def bad_factory(**kwargs: Any) -> Any:
                raise RuntimeError("simulated factory failure")

            runner = _make_runner(
                tmp_path,
                scratchpad_factory=lambda spec, sid: scratchpad,
                offloader_factory=bad_factory,
            )
            await runner._drive(
                spec=spec, prompt="do the thing", wt=wt, stream=False,
                session_id="sess-Z",
            )
            # Runtime was still constructed, with tool_offloader=None.
            assert captured["kw"].get("tool_offloader") is None
        finally:
            runner_mod.ToolRuntime = original


# === SpyToolRuntime signature sync ===

class TestRuntimeOffloadKwarg:
    def test_toolruntime_accepts_tool_offloader_kwarg(
        self, tmp_path: Path,
    ) -> None:
        """The ToolRuntime signature must accept ``tool_offloader=``
        without raising TypeError."""
        from harness.server.agent.runtime import ToolRuntime
        # If the kwarg is missing from the signature, this raises TypeError.
        ToolRuntime(tmp_path, tool_offloader=None)


# === SpyToolRuntime via _drive (integration) ===

class TestRunnerOffloadIntegration:
    async def test_tool_offloader_reaches_toolruntime(
        self, tmp_path: Path, spec: Any, wt: Any,
    ) -> None:
        """The offloader produced by the factory must reach the
        ToolRuntime constructor in both _drive and _stream_drive."""
        from harness.server.agent.runtime import ToolRuntime

        captured: list[dict[str, Any]] = []
        real_TR = ToolRuntime

        class SpyToolRuntime(real_TR):
            def __init__(self, *a: Any, **kw: Any) -> None:
                captured.append(kw)
                super().__init__(*a, **kw)

        import harness.agents.runner as runner_mod
        original = runner_mod.ToolRuntime
        runner_mod.ToolRuntime = SpyToolRuntime  # type: ignore[assignment]
        try:
            fake_offloader = MagicMock()
            fake_offloader._scratchpad = None
            fake_offloader._settings = MagicMock(tool_offload_max_ms=2000)
            scratchpad = FakeScratchpad(session_id="sess-W")

            runner = _make_runner(
                tmp_path,
                scratchpad_factory=lambda spec, sid: scratchpad,
                offloader_factory=lambda **kw: fake_offloader,
            )
            # _drive (sync path) wires the offloader.
            await runner._drive(
                spec=spec, prompt="do the thing", wt=wt, stream=False,
                session_id="sess-W",
            )
            assert captured[-1]["tool_offloader"] is fake_offloader
        finally:
            runner_mod.ToolRuntime = original
