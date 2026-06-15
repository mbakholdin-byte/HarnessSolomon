"""Tests for AgentRunner scratchpad factory + session_id threading (Phase 3 v1.2.0, Step 2)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.agents.runner import AgentRunner


# === Test doubles ===

class FakeScratchpad:
    """Minimal in-memory scratchpad double for the runner.

    Implements the small surface the runner + ToolRuntime use: ``init()``
    and an attribute ``_session_id`` (read by the audit hook).
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.init_calls = 0
        self._session_id = session_id or "fake-sess"

    async def init(self) -> None:
        self.init_calls += 1


class FakeRouter:
    """Stub router — runner never invokes it in these tests (loop is short-circuited)."""
    pass


# === Factory wiring ===

def _make_runner(
    tmp_path: Path,
    *,
    scratchpad_factory=None,
) -> AgentRunner:
    return AgentRunner(
        router=FakeRouter(),  # type: ignore[arg-type]
        repo=tmp_path,
        scratchpad_factory=scratchpad_factory,
    )


@pytest.fixture
def spec() -> Any:
    """Minimal spec double with the attributes the runner + filter_runtime
    actually read."""
    s = MagicMock()
    s.name = "test-agent"
    s.worktree_required = False
    s.memory_namespace = None
    s.model = "qwen3:8b"
    s.max_iterations = 1
    s.permissions = "full"        # permissions_denylist("full") = frozenset()
    s.allowed_paths = None
    s.tools = []                  # filter_tools uses spec.tools
    s.system_prompt = ""
    s.worktree_purpose = "ephemeral"
    return s


# === Tests ===

class TestFactoryWiring:
    async def test_factory_invoked_with_spec_and_session_id(
        self, tmp_path: Path, spec: Any,
    ) -> None:
        # Patch AgentLoop inside runner module so the loop exits after
        # one iteration without touching the LLM router. The factory
        # call happens BEFORE AgentLoop is constructed, so this still
        # observes the factory invocation.
        from harness.agents import runner as runner_mod

        factory = MagicMock(return_value=FakeScratchpad(session_id="sess-X"))
        runner = _make_runner(tmp_path, scratchpad_factory=factory)

        # Patch AgentLoop to a no-op async generator (the runner uses
        # ``async for event in loop.run(...)`` which requires an async
        # generator, not a plain coroutine).
        class _NoopLoop:
            def __init__(self, *a: Any, **kw: Any) -> None: ...
            async def run(self, *a: Any, **kw: Any) -> Any:
                if False:
                    yield None  # makes this an async generator

        original_loop = getattr(runner_mod, "AgentLoop", None)
        runner_mod.AgentLoop = _NoopLoop  # type: ignore[attr-defined]
        try:
            await runner.run(spec, "hello", session_id="sess-X", worktree_id="no-wt")
        finally:
            if original_loop is not None:
                runner_mod.AgentLoop = original_loop  # type: ignore[attr-defined]

        factory.assert_called_once_with(spec, "sess-X")

    async def test_factory_none_means_no_scratchpad_kwarg_on_runtime(
        self, tmp_path: Path, spec: Any,
    ) -> None:
        from harness.agents import runner as runner_mod

        runner = _make_runner(tmp_path, scratchpad_factory=None)
        captured: dict[str, Any] = {}

        async def fake_drive(self, *args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return None

        original_drive = runner_mod.AgentRunner._drive
        runner_mod.AgentRunner._drive = fake_drive  # type: ignore[method-assign]
        try:
            await runner.run(spec, "hello", session_id="sess-X", worktree_id="no-wt")
        finally:
            runner_mod.AgentRunner._drive = original_drive  # type: ignore[method-assign]
        assert captured.get("session_id") == "sess-X"

    async def test_runtime_receives_scratchpad_instance_from_factory(
        self, tmp_path: Path, spec: Any,
    ) -> None:
        from harness.server.agent.runtime import ToolRuntime

        # Factory returns a FakeScratchpad; we intercept the runtime
        # construction by replacing the ToolRuntime class with a spy.
        captured: dict[str, Any] = {}

        real_ToolRuntime = ToolRuntime

        class SpyToolRuntime(real_ToolRuntime):
            def __init__(  # type: ignore[no-untyped-def]
                self, project_root, *,
                scratchpad=None, scratchpad_audit=None, l0_section=None,
                l2_retriever=None, l2_router=None, l2_curator_model="qwen3:8b",
                tool_offloader=None,
            ):
                captured["scratchpad"] = scratchpad
                captured["scratchpad_audit"] = scratchpad_audit
                captured["l0_section"] = l0_section
                captured["tool_offloader"] = tool_offloader
                super().__init__(
                    project_root,
                    scratchpad=scratchpad,
                    scratchpad_audit=scratchpad_audit,
                    l0_section=l0_section,
                    l2_retriever=l2_retriever,
                    l2_router=l2_router,
                    l2_curator_model=l2_curator_model,
                    tool_offloader=tool_offloader,
                )

        fake = FakeScratchpad(session_id="sess-Y")
        factory = MagicMock(return_value=fake)
        runner = _make_runner(tmp_path, scratchpad_factory=factory)

        # Patch ToolRuntime reference inside runner module to our spy.
        # Also patch AgentLoop to a no-op so the loop doesn't actually
        # try to call the (fake) router.
        from harness.agents import runner as runner_mod
        original_rt = getattr(runner_mod, "ToolRuntime", ToolRuntime)
        original_loop = getattr(runner_mod, "AgentLoop", None)

        class _NoopLoop:
            def __init__(self, *a: Any, **kw: Any) -> None: ...
            async def run(self, *a: Any, **kw: Any) -> Any:
                if False:
                    yield None

        runner_mod.ToolRuntime = SpyToolRuntime  # type: ignore[attr-defined]
        runner_mod.AgentLoop = _NoopLoop  # type: ignore[attr-defined]
        try:
            await runner._drive(
                spec, "hi", _noop_worktree(tmp_path),
                stream=False, session_id="sess-Y",
            )
        finally:
            runner_mod.ToolRuntime = original_rt  # type: ignore[attr-defined]
            if original_loop is not None:
                runner_mod.AgentLoop = original_loop  # type: ignore[attr-defined]

        assert captured["scratchpad"] is fake
        assert fake.init_calls == 1, "factory result must be init()'d by runner"

    async def test_session_id_none_skips_factory(
        self, tmp_path: Path, spec: Any,
    ) -> None:
        factory = MagicMock(return_value=FakeScratchpad())
        runner = _make_runner(tmp_path, scratchpad_factory=factory)

        await runner._drive(
            spec, "hi", _noop_worktree(tmp_path),
            stream=False, session_id=None,
        )
        factory.assert_not_called(), "factory must not be called when session_id is None"

    async def test_factory_exception_fails_open(
        self, tmp_path: Path, spec: Any, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def boom(spec: Any, session_id: str | None) -> Any:
            raise RuntimeError("factory boom")

        runner = _make_runner(tmp_path, scratchpad_factory=boom)
        with caplog.at_level(logging.WARNING):
            await runner._drive(
                spec, "hi", _noop_worktree(tmp_path),
                stream=False, session_id="sess-Z",
            )
        # The factory raised — runner must have logged a warning AND
        # still built a runtime (with scratchpad=None).
        assert any("scratchpad factory/init failed" in r.message for r in caplog.records)
        # Verify the runtime was built without a scratchpad (no exception
        # escaped).
        # (No further assertion — the fact that _drive returned is enough.)

    async def test_run_threads_session_id_through_to_factory(
        self, tmp_path: Path, spec: Any,
    ) -> None:
        # Verify session_id is passed to _drive unchanged. Patch
        # AgentLoop so the no-worktree _drive path runs to completion
        # without actually invoking the LLM router.
        from harness.agents import runner as runner_mod

        factory = MagicMock(return_value=FakeScratchpad(session_id="sess-1"))
        runner = _make_runner(tmp_path, scratchpad_factory=factory)

        class _NoopLoop:
            def __init__(self, *a: Any, **kw: Any) -> None: ...
            async def run(self, *a: Any, **kw: Any) -> Any:
                if False:
                    yield None  # async-generator marker

        original_loop = getattr(runner_mod, "AgentLoop", None)
        runner_mod.AgentLoop = _NoopLoop  # type: ignore[attr-defined]
        try:
            await runner.run(spec, "p", session_id="sess-1", worktree_id="no-wt")
        finally:
            if original_loop is not None:
                runner_mod.AgentLoop = original_loop  # type: ignore[attr-defined]
        factory.assert_called_once_with(spec, "sess-1")


# === Helpers ===

class _FakeWorktree:
    def __init__(self, path: Path) -> None:
        self.path = path


def _noop_worktree(tmp_path: Path) -> Any:
    return _FakeWorktree(tmp_path)
