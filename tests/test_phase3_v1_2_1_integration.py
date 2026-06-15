"""End-to-end integration tests for Phase 3 v1.2.1 L0 → system prompt injection.

Covers the full flow:
  * ``write_note(L0)`` → ``AgentRunner.run()`` → messages[0] contains L0
  * empty L0 → no "## Hot context" in messages[0]
  * setting ``scratchpad_inject_l0_to_system_prompt=False`` → no injection
  * ``read_notes`` raises → L0 section absent, chat loop completes (fail-open)

These tests use a real ``ScratchpadStore`` on a tmp SQLite DB (no
mocking of the store), so they exercise the same code path the
production ``lifespan`` does.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.agents.runner import AgentRunner
from harness.agents.scratchpad_store import ScratchpadStore
from harness.agents.spec import AgentSpec
from harness.config import settings
from harness.server.agent.runtime import ToolRuntime
from harness.server.llm.router import CompletionResult, StreamEvent


# === Fakes ===

class FakeRouter:
    """Records the messages sent to the LLM so we can assert on them."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def completion(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        self.calls.append(
            {"messages": list(messages), "model": model, "tools": tools}
        )
        return CompletionResult(content="ok", tool_calls=None, usage={}, cost=0.0)

    # No streaming support — loop auto-falls back to completion().


def _make_runner(
    tmp_path: Path,
    scratchpad_factory: Any,
) -> AgentRunner:
    return AgentRunner(
        router=FakeRouter(),  # type: ignore[arg-type]
        repo=tmp_path,
        scratchpad_factory=scratchpad_factory,
    )


@pytest.fixture
def spec() -> AgentSpec:
    """An AgentSpec that allows read_file + scratchpad tools (so the
    runtime's filter_tools doesn't strip everything out)."""
    return AgentSpec(
        name="test-agent",
        model="qwen3:8b",
        tools=["read_file", "scratchpad_read_notes"],
        permissions="full",
        max_iterations=1,
        system_prompt="",
    )


# === Helpers ===

def _noop_worktree(path: Path) -> Any:
    class _Wt:
        def __init__(self, p: Path) -> None:
            self.path = p
    return _Wt(path)


async def _drive_capture_messages(
    runner: AgentRunner, spec: AgentSpec, tmp_path: Path, session_id: str,
) -> list[dict[str, Any]]:
    """Drive a single run and return the messages the LLM saw."""
    await runner._drive(
        spec, "hi", _noop_worktree(tmp_path),
        stream=False, session_id=session_id,
    )
    # The runner constructs a fresh FakeRouter via _make_runner, but
    # since FakeRouter state is per-instance we have to fish it from
    # the captured messages through the runner's own router attr.
    return runner.router.calls[0]["messages"]   # type: ignore[attr-defined]


# === Tests ===

class TestE2EL0Injection:
    async def test_e2e_l0_injection_through_runner(
        self, tmp_path: Path, spec: AgentSpec,
    ) -> None:
        """Write 3 L0 notes → runner.run() → messages[0] contains all 3."""
        # Real scratchpad on a tmp DB.
        store = ScratchpadStore(
            db_path=tmp_path / "agent-jobs.db",
            session_id="sess-l0-e2e",
            agent_id="test-agent",
        )
        await store.init()
        await store.write_note("L0", "user prefers concise replies", tags=["pref"])
        await store.write_note("L0", "always reply in Russian", tags=["pref", "lang"])
        await store.write_note("L0", "current plan: ship v1.2.1", tags=["plan"])

        def factory(s: AgentSpec, sid: str | None) -> ScratchpadStore:
            assert sid == "sess-l0-e2e"
            return store

        runner = _make_runner(tmp_path, scratchpad_factory=factory)
        messages = await _drive_capture_messages(
            runner, spec, tmp_path, "sess-l0-e2e",
        )

        sys_msg = messages[0]
        assert sys_msg["role"] == "system"
        assert "## Hot context" in sys_msg["content"]
        assert "user prefers concise replies" in sys_msg["content"]
        assert "always reply in Russian" in sys_msg["content"]
        assert "current plan: ship v1.2.1" in sys_msg["content"]
        # And the standard prelude is still there.
        assert "You are Solomon" in sys_msg["content"]

    async def test_e2e_l0_injection_empty_skipped(
        self, tmp_path: Path, spec: AgentSpec,
    ) -> None:
        """No L0 notes → messages[0] does NOT contain "## Hot context"."""
        store = ScratchpadStore(
            db_path=tmp_path / "agent-jobs.db",
            session_id="sess-l0-empty",
            agent_id="test-agent",
        )
        await store.init()
        # No write_note calls — L0 is empty.

        def factory(s: AgentSpec, sid: str | None) -> ScratchpadStore:
            return store

        runner = _make_runner(tmp_path, scratchpad_factory=factory)
        messages = await _drive_capture_messages(
            runner, spec, tmp_path, "sess-l0-empty",
        )

        sys_msg = messages[0]
        assert sys_msg["role"] == "system"
        assert "## Hot context" not in sys_msg["content"]
        assert "You are Solomon" in sys_msg["content"]

    async def test_e2e_l0_injection_disabled_by_setting(
        self, tmp_path: Path, spec: AgentSpec, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``scratchpad_inject_l0_to_system_prompt=False`` → no injection."""
        monkeypatch.setattr(settings, "scratchpad_inject_l0_to_system_prompt", False)

        store = ScratchpadStore(
            db_path=tmp_path / "agent-jobs.db",
            session_id="sess-l0-off",
            agent_id="test-agent",
        )
        await store.init()
        await store.write_note("L0", "this should NOT appear in system prompt")

        def factory(s: AgentSpec, sid: str | None) -> ScratchpadStore:
            return store

        runner = _make_runner(tmp_path, scratchpad_factory=factory)
        messages = await _drive_capture_messages(
            runner, spec, tmp_path, "sess-l0-off",
        )

        sys_msg = messages[0]
        assert "## Hot context" not in sys_msg["content"]
        assert "this should NOT appear" not in sys_msg["content"]

    async def test_e2e_l0_read_exception_fails_open(
        self, tmp_path: Path, spec: AgentSpec, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """scratchpad.read_notes raises → L0 section absent, chat completes."""
        # Store with a busted read_notes (raises on call).
        class _BrokenStore:
            _session_id = "sess-broken"

            async def init(self) -> None: ...

            async def read_notes(self, *args: Any, **kw: Any) -> Any:
                raise RuntimeError("simulated DB failure")

        def factory(s: AgentSpec, sid: str | None) -> Any:
            return _BrokenStore()

        import logging
        runner = _make_runner(tmp_path, scratchpad_factory=factory)
        with caplog.at_level(logging.WARNING):
            messages = await _drive_capture_messages(
                runner, spec, tmp_path, "sess-broken",
            )

        # L0 section absent — fail-open.
        assert "## Hot context" not in messages[0]["content"]
        assert "You are Solomon" in messages[0]["content"]
        # Warning was logged.
        assert any("L0 read failed" in r.message for r in caplog.records)


# === Bonus: runner wires l0_section into ToolRuntime ===

class TestRunnerWiresL0SectionIntoRuntime:
    async def test_runner_injects_l0_section_into_runtime(
        self, tmp_path: Path,
    ) -> None:
        """runner with scratchpad containing 1 L0 note → runtime._l0_section
        is set to the formatted string (so a direct AgentLoop caller
        also gets injection via the defence-in-depth path)."""
        # Minimal in-memory double — we only need read_notes("L0", ...).
        class _MemStore:
            _session_id = "sess-runtime"

            def __init__(self) -> None:
                self.init_calls = 0
                self._l0 = [
                    _make_note(id=1, content="hot fact", tags=["t"]),
                ]

            async def init(self) -> None:
                self.init_calls += 1

            async def read_notes(self, level: Any = None, *, limit: int = 100) -> list[Any]:
                return list(self._l0)[:limit]

        def _make_note(*, id: int, content: str, tags: list[str]) -> Any:
            n = MagicMock()
            n.id = id
            n.content = content
            n.tags = tags
            return n

        store = _MemStore()
        captured: dict[str, Any] = {}

        real_rt = ToolRuntime

        class _SpyRT(real_rt):  # type: ignore[misc]
            def __init__(  # type: ignore[no-untyped-def]
                self, project_root, *,
                scratchpad=None, scratchpad_audit=None, l0_section=None,
            ):
                captured["l0_section"] = l0_section
                super().__init__(
                    project_root,
                    scratchpad=scratchpad,
                    scratchpad_audit=scratchpad_audit,
                    l0_section=l0_section,
                )

        import logging
        from harness.agents import runner as runner_mod
        original_rt = getattr(runner_mod, "ToolRuntime", ToolRuntime)
        original_loop = getattr(runner_mod, "AgentLoop", None)

        class _NoopLoop:
            def __init__(self, *a: Any, **kw: Any) -> None: ...
            async def run(self, *a: Any, **kw: Any) -> Any:
                if False:
                    yield None

        runner_mod.ToolRuntime = _SpyRT  # type: ignore[assignment]
        runner_mod.AgentLoop = _NoopLoop  # type: ignore[assignment]
        try:
            runner = AgentRunner(
                router=FakeRouter(),  # type: ignore[arg-type]
                repo=tmp_path,
                scratchpad_factory=lambda s, sid: store,
            )
            s = MagicMock()
            s.name = "x"
            s.worktree_required = False
            s.model = "qwen3:8b"
            s.max_iterations = 1
            s.permissions = "full"
            s.allowed_paths = None
            s.tools = ["read_file"]
            s.system_prompt = ""
            s.worktree_purpose = "ephemeral"
            await runner._drive(
                s, "hi", _noop_worktree(tmp_path),
                stream=False, session_id="sess-runtime",
            )
        finally:
            runner_mod.ToolRuntime = original_rt  # type: ignore[assignment]
            if original_loop is not None:
                runner_mod.AgentLoop = original_loop  # type: ignore[assignment]

        assert captured["l0_section"] is not None
        assert "## Hot context" in captured["l0_section"]
        assert "hot fact" in captured["l0_section"]
