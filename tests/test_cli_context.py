"""Tests for ``harness context`` CLI subcommand (Phase 3 v1.2.0, Step 3)."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from harness.agents.scratchpad import NoteLevel, PlanStatus
from harness.agents.scratchpad_store import ScratchpadStore
from harness.cli import _build_parser


# === Parser registration ===

class TestParserRegistration:
    def test_parser_registers_context_subcommand(self) -> None:
        parser = _build_parser()
        # ``harness context`` should parse without error.
        ns = parser.parse_args(["context", "read", "--session", "x"])
        assert ns.func is not None
        assert ns.context_command == "read"
        assert ns.session == "x"

    def test_parser_subcommands(self) -> None:
        parser = _build_parser()
        ns = parser.parse_args(["context", "plan", "--session", "y", "--mark-done", "--step-id", "5"])
        assert ns.context_command == "plan"
        assert ns.mark_done is True
        assert ns.step_id == 5


# === Direct handler tests (in-process) ===

class _CliRunner:
    """Helper to invoke the CLI handlers in-process without spawning a subprocess."""

    @staticmethod
    def read(session: str, agent: str | None = None, level: str | None = None) -> int:
        ns = _build_parser().parse_args([
            "context", "read", "--session", session,
            *(["--agent", agent] if agent else []),
            *(["--level", level] if level else []),
        ])
        from harness.cli import _cmd_context_read
        return _cmd_context_read(ns)

    @staticmethod
    def write(session: str, level: str, content: str, tags: str = "") -> int:
        ns = _build_parser().parse_args([
            "context", "write", "--session", session, "--level", level,
            "--content", content, "--tags", tags,
        ])
        from harness.cli import _cmd_context_write
        return _cmd_context_write(ns)

    @staticmethod
    def plan_list(session: str, agent: str | None = None) -> int:
        ns = _build_parser().parse_args([
            "context", "plan", "--session", session,
            *(["--agent", agent] if agent else []),
        ])
        from harness.cli import _cmd_context_plan
        return _cmd_context_plan(ns)

    @staticmethod
    def plan_mark_done(session: str, step_id: int, status: str = "done") -> int:
        ns = _build_parser().parse_args([
            "context", "plan", "--session", session,
            "--mark-done", "--step-id", str(step_id), "--status", status,
        ])
        from harness.cli import _cmd_context_plan
        return _cmd_context_plan(ns)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point settings.db_path at a temp file so the CLI doesn't touch
    the real harness.db. Autouse to keep the test CLI isolated."""
    from harness.config import settings
    monkeypatch.setattr(settings, "db_path", tmp_path / "harness.db", raising=False)


class TestCliBehavior:
    def test_context_read_empty_session_exits_zero(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = _CliRunner.read("nonexistent")
        assert rc == 0
        captured = capsys.readouterr()
        assert "(no notes)" in captured.err

    def test_context_write_persists_note(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Pre-seed: ensure the same agent-jobs.db the CLI writes to is
        # the one the test reads from.
        from harness.config import settings
        db_path = settings.db_path.parent / "agent-jobs.db"
        # Write via the CLI handler.
        rc = _CliRunner.write("sess-cli", "L1", "hello world", tags="a,b")
        assert rc == 0
        captured = capsys.readouterr()
        assert "wrote note" in captured.out
        assert "level=L1" in captured.out

        # Verify on the store directly.
        store = ScratchpadStore(db_path, session_id="sess-cli", agent_id=None)
        notes = asyncio_run(store.read_notes(NoteLevel.L1))
        assert len(notes) == 1
        assert notes[0].content == "hello world"
        assert notes[0].tags == ["a", "b"]

    def test_context_plan_lists_steps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from harness.config import settings
        db_path = settings.db_path.parent / "agent-jobs.db"
        # Seed via the store.
        store = ScratchpadStore(db_path, session_id="sess-plan", agent_id=None)
        s1 = asyncio_run(store.add_plan_step("first"))
        s2 = asyncio_run(store.add_plan_step("second", deps=[s1.id]))

        rc = _CliRunner.plan_list("sess-plan")
        assert rc == 0
        captured = capsys.readouterr()
        assert "first" in captured.out
        assert "second" in captured.out
        # Newest is "second" → ordered by created_at ASC so first first
        # but the table includes both.
        assert "deps" in captured.out  # column header is present

    def test_context_plan_mark_done_updates_status(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from harness.config import settings
        db_path = settings.db_path.parent / "agent-jobs.db"
        store = ScratchpadStore(db_path, session_id="sess-done", agent_id=None)
        s1 = asyncio_run(store.add_plan_step("to be done"))

        rc = _CliRunner.plan_mark_done("sess-done", s1.id)
        assert rc == 0
        captured = capsys.readouterr()
        assert "marked step" in captured.out
        assert "done" in captured.out

        # Verify on the store.
        steps = asyncio_run(store.list_plan_steps(status=PlanStatus.DONE))
        assert len(steps) == 1
        assert steps[0].id == s1.id


class TestCliHelp:
    def test_context_help_renders(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``harness context --help`` exits 0 and lists subcommands."""
        from harness.cli import main
        # Use SystemExit capture via parser help.
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(["context", "--help"])
        assert exc_info.value.code == 0


# === Helpers ===

def asyncio_run(coro: object) -> object:
    """Run an awaitable in a one-shot loop. Sync helper for tests."""
    import asyncio
    return asyncio.run(coro)  # type: ignore[arg-type]
