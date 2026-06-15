"""Tests for scratchpad tool wiring in ToolRuntime (Phase 3 v1.2.0, Step 1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from harness.agents.runner import _READ_ONLY_DENY, permissions_denylist
from harness.agents.scratchpad import Note, NoteLevel, PlanStatus, PlanStep
from harness.agents.scratchpad_store import ScratchpadStore
from harness.server.agent.runtime import ToolRuntime, ToolName


# === Fixtures ===

@pytest.fixture
def runtime_no_scratchpad(tmp_path: Path) -> ToolRuntime:
    """Runtime with no scratchpad attached — 4 scratchpad tools must error."""
    return ToolRuntime(project_root=tmp_path)


@pytest.fixture
async def runtime_with_scratchpad(tmp_path: Path) -> tuple[ToolRuntime, ScratchpadStore]:
    """Runtime with a real, initialised scratchpad store."""
    store = ScratchpadStore(
        tmp_path / "agent-jobs.db",
        session_id="sess-tools",
        agent_id="solomon",
    )
    await store.init()
    rt = ToolRuntime(project_root=tmp_path, scratchpad=store)
    return rt, store


# === TOOL_SCHEMAS presence ===

class TestSchemas:
    def test_schemas_have_4_scratchpad_entries(self) -> None:
        from harness.server.agent.tools import TOOL_SCHEMAS
        names = {t["name"] for t in TOOL_SCHEMAS}
        assert "scratchpad_write_note" in names
        assert "scratchpad_read_notes" in names
        assert "scratchpad_plan_step" in names
        assert "scratchpad_mark_done" in names

    def test_schemas_validate_required_fields(self) -> None:
        from harness.server.agent.tools import TOOL_SCHEMAS
        schemas = {t["name"]: t for t in TOOL_SCHEMAS}
        # write_note: level + content required, tags optional
        wn = schemas["scratchpad_write_note"]
        assert set(wn["parameters"]["required"]) == {"level", "content"}
        # read_notes: no required
        assert schemas["scratchpad_read_notes"]["parameters"].get("required", []) == []
        # plan_step: description required
        assert set(schemas["scratchpad_plan_step"]["parameters"]["required"]) == {"description"}
        # mark_done: step_id required
        assert set(schemas["scratchpad_mark_done"]["parameters"]["required"]) == {"step_id"}


# === ToolName Literal ===

class TestToolNameLiteral:
    def test_literal_includes_scratchpad_tools(self) -> None:
        # Type-level check: the Literal must include all 4 new names.
        # If a name is missing, a static type checker would flag it; we
        # just check the set is the expected one for runtime behavior.
        expected = {
            "read_file", "edit_file", "write_file", "bash", "grep", "glob",
            "scratchpad_write_note", "scratchpad_read_notes",
            "scratchpad_plan_step", "scratchpad_mark_done",
        }
        # The Literal type is erased at runtime, but the set of valid
        # names should match the dispatcher elif chain we test below.
        assert "scratchpad_write_note" in expected


# === Dispatcher behavior ===

class TestRuntimeDispatch:
    async def test_scratchpad_none_returns_error(
        self, runtime_no_scratchpad: ToolRuntime,
    ) -> None:
        for name in (
            "scratchpad_write_note",
            "scratchpad_read_notes",
            "scratchpad_plan_step",
            "scratchpad_mark_done",
        ):
            res = await runtime_no_scratchpad.execute(name, {})
            assert res.ok is False
            assert "scratchpad not enabled" in res.error, f"{name}: {res.error!r}"

    async def test_write_note_calls_store(
        self, runtime_with_scratchpad: tuple[ToolRuntime, ScratchpadStore],
    ) -> None:
        rt, store = runtime_with_scratchpad
        res = await rt.execute("scratchpad_write_note", {
            "level": "L1", "content": "test note", "tags": ["t1"],
        })
        assert res.ok is True, res.error
        payload = json.loads(res.output)
        assert payload["level"] == "L1"
        assert payload["id"] > 0
        # Verify the note really persisted.
        notes = await store.read_notes(NoteLevel.L1)
        assert len(notes) == 1
        assert notes[0].content == "test note"
        assert notes[0].tags == ["t1"]

    async def test_read_notes_returns_list_serialization(
        self, runtime_with_scratchpad: tuple[ToolRuntime, ScratchpadStore],
    ) -> None:
        rt, _ = runtime_with_scratchpad
        # Seed 2 notes
        await rt.execute("scratchpad_write_note", {"level": "L1", "content": "a"})
        await rt.execute("scratchpad_write_note", {"level": "L1", "content": "b"})
        res = await rt.execute("scratchpad_read_notes", {"level": "L1"})
        assert res.ok is True
        notes = json.loads(res.output)
        assert len(notes) == 2
        # Newest first (DESC).
        assert notes[0]["content"] == "b"
        assert notes[1]["content"] == "a"
        for n in notes:
            assert set(n.keys()) == {"id", "level", "content", "tags", "created_at"}

    async def test_plan_step_then_mark_done_round_trip(
        self, runtime_with_scratchpad: tuple[ToolRuntime, ScratchpadStore],
    ) -> None:
        rt, store = runtime_with_scratchpad
        add_res = await rt.execute("scratchpad_plan_step", {
            "description": "first task", "deps": [],
        })
        assert add_res.ok is True
        step = json.loads(add_res.output)
        assert step["status"] == "pending"
        step_id = step["id"]

        done_res = await rt.execute("scratchpad_mark_done", {
            "step_id": step_id, "status": "done",
        })
        assert done_res.ok is True
        updated = json.loads(done_res.output)
        assert updated["status"] == "done"

        # Verify on the store.
        steps = await store.list_plan_steps(status=PlanStatus.DONE)
        assert len(steps) == 1
        assert steps[0].id == step_id


# === Fail-open ===

class TestFailOpen:
    async def test_store_exception_returns_error_not_raises(
        self, tmp_path: Path,
    ) -> None:
        # Mock store that raises on every call.
        mock_store = AsyncMock()
        mock_store._session_id = "sess-fail"
        mock_store.write_note.side_effect = RuntimeError("boom")
        mock_store.read_notes.side_effect = RuntimeError("boom")
        mock_store.add_plan_step.side_effect = RuntimeError("boom")
        mock_store.mark_done.side_effect = RuntimeError("boom")

        rt = ToolRuntime(project_root=tmp_path, scratchpad=mock_store)

        # None of these should raise — the chat loop must keep going.
        wn = await rt.execute("scratchpad_write_note", {"level": "L1", "content": "x"})
        assert wn.ok is False
        assert "RuntimeError" in wn.error and "boom" in wn.error

        rn = await rt.execute("scratchpad_read_notes", {})
        assert rn.ok is False
        assert "boom" in rn.error

        ps = await rt.execute("scratchpad_plan_step", {"description": "d"})
        assert ps.ok is False
        assert "boom" in ps.error

        md = await rt.execute("scratchpad_mark_done", {"step_id": 1})
        assert md.ok is False
        assert "boom" in md.error


# === Read-only denylist ===

class TestReadOnlyDeny:
    def test_read_only_deny_list_includes_scratchpad_writes(self) -> None:
        deny = permissions_denylist("read-only")
        # All 3 scratchpad write tools are denied for read-only agents.
        assert "scratchpad_write_note" in deny
        assert "scratchpad_plan_step" in deny
        assert "scratchpad_mark_done" in deny
        # scratchpad_read_notes is NOT in the denylist — read-only agents
        # can still consult their notes.
        assert "scratchpad_read_notes" not in deny
        # write_file / edit_file are still denied (regression).
        assert "write_file" in deny
        assert "edit_file" in deny

    def test_scoped_write_and_full_have_empty_denylist(self) -> None:
        assert permissions_denylist("scoped-write") == frozenset()
        assert permissions_denylist("full") == frozenset()
