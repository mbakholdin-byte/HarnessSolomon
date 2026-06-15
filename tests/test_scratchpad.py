"""Tests for :mod:`harness.agents.scratchpad` + :mod:`harness.agents.scratchpad_store` (Phase 3 v1.2.0, Step 0)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from harness.agents.scratchpad import (
    Note,
    NoteLevel,
    PlanStatus,
    PlanStep,
)
from harness.agents.scratchpad_store import (
    DEFAULT_L0_MAX_BYTES,
    ScratchpadStore,
)


# === Fixtures ===

@pytest.fixture
def tmp_store(tmp_path: Path) -> ScratchpadStore:
    """Return a fresh, uninitialised ``ScratchpadStore`` in a temp dir."""
    return ScratchpadStore(
        tmp_path / "agent-jobs.db",
        session_id="sess-test",
        agent_id="solomon",
    )


@pytest.fixture
def tmp_store_admin(tmp_path: Path) -> ScratchpadStore:
    """Return a store bound to ``agent_id=None`` (admin / cross-agent)."""
    return ScratchpadStore(
        tmp_path / "agent-jobs.db",
        session_id="sess-admin",
        agent_id=None,
    )


@pytest.fixture
def tmp_store_tiny_l0(tmp_path: Path) -> ScratchpadStore:
    """Return a store with a tiny L0 cap for cap-enforcement tests."""
    return ScratchpadStore(
        tmp_path / "agent-jobs.db",
        session_id="sess-tiny",
        agent_id="solomon",
        l0_max_bytes=128,
    )


# === Dataclass marshalling ===

class TestNoteDataclass:
    def test_to_row_serialises_all_fields(self) -> None:
        now = time.time()
        note = Note(
            session_id="s1", agent_id="a1", level=NoteLevel.L1,
            content="hello", tags=["k1", "k2"], created_at=now,
        )
        row = note.to_row()
        # Verify the SQL column shape directly (no DB round-trip needed
        # for a pure marshalling test — the public write/read path is
        # covered by TestWriteReadNote).
        assert row["session_id"] == "s1"
        assert row["agent_id"] == "a1"
        assert row["level"] == "L1"
        assert row["content"] == "hello"
        assert row["created_at"] == now
        # tags are stored as a JSON string.
        import json as _json
        assert _json.loads(row["tags"]) == ["k1", "k2"]

    def test_from_row_handles_json_tags(self) -> None:
        # Build a sqlite3.Row with the columns from_row expects.
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute(
                "CREATE TABLE scratchpad_notes ("
                "  id INTEGER PRIMARY KEY, session_id TEXT, agent_id TEXT,"
                "  level TEXT, content TEXT, tags TEXT, created_at REAL"
                ")"
            )
            cur = db.execute(
                "INSERT INTO scratchpad_notes "
                "(session_id, agent_id, level, content, tags, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
                ("s", "a", "L2", "body", '["x","y"]', 1.5),
            )
            row = cur.fetchone()
        note = Note.from_row(row)
        assert note.id > 0
        assert note.session_id == "s"
        assert note.agent_id == "a"
        assert note.level == NoteLevel.L2
        assert note.content == "body"
        assert note.tags == ["x", "y"]
        assert note.created_at == 1.5


class TestPlanStepDataclass:
    def test_deps_default_to_empty_list(self) -> None:
        step = PlanStep(session_id="s", description="d")
        assert step.deps == []
        assert step.status == PlanStatus.PENDING

    def test_from_row_handles_status_enum(self) -> None:
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            db.execute(
                "CREATE TABLE plan_steps ("
                "  id INTEGER PRIMARY KEY, session_id TEXT, agent_id TEXT,"
                "  description TEXT, status TEXT, deps TEXT,"
                "  created_at REAL, updated_at REAL"
                ")"
            )
            cur = db.execute(
                "INSERT INTO plan_steps "
                "(session_id, agent_id, description, status, deps, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "RETURNING *",
                ("s", None, "desc", "blocked", "[1,2]", 1.0, 1.0),
            )
            row = cur.fetchone()
        step = PlanStep.from_row(row)
        assert step.status == PlanStatus.BLOCKED
        assert step.deps == [1, 2]
        assert step.agent_id is None


# === Schema + init ===

class TestStoreInit:
    async def test_init_creates_both_tables(self, tmp_store: ScratchpadStore) -> None:
        await tmp_store.init()
        async with __import__("aiosqlite").connect(tmp_store._db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name",
            ) as cur:
                rows = await cur.fetchall()
        table_names = {r[0] for r in rows}
        assert "scratchpad_notes" in table_names
        assert "plan_steps" in table_names

    async def test_init_creates_indexes(self, tmp_store: ScratchpadStore) -> None:
        await tmp_store.init()
        async with __import__("aiosqlite").connect(tmp_store._db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('idx_notes_session_level', 'idx_plans_session_status')",
            ) as cur:
                rows = await cur.fetchall()
        assert len(rows) == 2

    async def test_init_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "agent-jobs.db"
        store = ScratchpadStore(nested, session_id="s", agent_id="a")
        await store.init()
        assert nested.parent.is_dir()
        assert nested.exists()

    async def test_init_idempotent(self, tmp_store: ScratchpadStore) -> None:
        await tmp_store.init()
        # Double init must not error.
        await tmp_store.init()
        assert await tmp_store.count() == 0


# === Notes: write + read ===

class TestWriteReadNote:
    async def test_write_then_read_round_trip(self, tmp_store: ScratchpadStore) -> None:
        await tmp_store.init()
        note = await tmp_store.write_note(
            NoteLevel.L1, "remember this", tags=["important"],
        )
        assert note.id > 0
        assert note.session_id == "sess-test"
        assert note.agent_id == "solomon"
        assert note.level == NoteLevel.L1
        assert note.content == "remember this"
        assert note.tags == ["important"]

        notes = await tmp_store.read_notes(NoteLevel.L1)
        assert len(notes) == 1
        assert notes[0].id == note.id
        assert notes[0].content == "remember this"

    async def test_read_filters_by_level(self, tmp_store: ScratchpadStore) -> None:
        await tmp_store.init()
        await tmp_store.write_note(NoteLevel.L0, "hot fact")
        await tmp_store.write_note(NoteLevel.L1, "plan fact")
        await tmp_store.write_note(NoteLevel.L2, "archive fact")

        l0 = await tmp_store.read_notes(NoteLevel.L0)
        l1 = await tmp_store.read_notes(NoteLevel.L1)
        l2 = await tmp_store.read_notes(NoteLevel.L2)
        all_levels = await tmp_store.read_notes()

        assert [n.content for n in l0] == ["hot fact"]
        assert [n.content for n in l1] == ["plan fact"]
        assert [n.content for n in l2] == ["archive fact"]
        assert {n.level for n in all_levels} == {NoteLevel.L0, NoteLevel.L1, NoteLevel.L2}


# === L0 cap enforcement ===

class TestL0Cap:
    async def test_l0_cap_enforced(
        self, tmp_store_tiny_l0: ScratchpadStore,
    ) -> None:
        # cap = 128 bytes; one 200-byte note alone exceeds the cap.
        await tmp_store_tiny_l0.init()
        with pytest.raises(ValueError, match="exceeds cap"):
            await tmp_store_tiny_l0.write_note(
                NoteLevel.L0, "x" * 200,
            )

    async def test_l0_oldest_pruned_to_fit(
        self, tmp_store_tiny_l0: ScratchpadStore,
    ) -> None:
        # cap = 128 bytes; 4 × 40-byte notes = 160 bytes total.
        # Auto-prune must keep the total at <= 128.
        await tmp_store_tiny_l0.init()
        ids = []
        for i in range(4):
            note = await tmp_store_tiny_l0.write_note(
                NoteLevel.L0, f"note-{i}-" + "x" * 30,  # 38 bytes incl. tag
            )
            ids.append(note.id)

        total = await tmp_store_tiny_l0.l0_size_bytes()
        assert total <= 128, f"L0 total {total} bytes exceeds cap 128"

        kept = await tmp_store_tiny_l0.read_notes(NoteLevel.L0)
        # The newest notes should survive (auto-prune is FIFO by created_at).
        assert kept[0].content.startswith("note-3-"), "newest L0 must survive"
        # Some of the older notes should have been pruned.
        assert len(kept) < 4, f"expected auto-prune, kept {len(kept)} of 4"


# === Misc ===

class TestConstants:
    def test_default_l0_max_bytes_is_1024(self) -> None:
        assert DEFAULT_L0_MAX_BYTES == 1024

    def test_constructor_rejects_missing_session_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="session_id is required"):
            ScratchpadStore(tmp_path / "x.db", session_id="", agent_id="a")

    def test_constructor_rejects_tiny_l0_cap(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="l0_max_bytes"):
            ScratchpadStore(
                tmp_path / "x.db", session_id="s", agent_id="a",
                l0_max_bytes=64,
            )


# === Plan step basics (sanity) ===

class TestPlanStepBasics:
    async def test_add_and_list_plan_steps(self, tmp_store: ScratchpadStore) -> None:
        await tmp_store.init()
        s1 = await tmp_store.add_plan_step("first step", deps=[])
        s2 = await tmp_store.add_plan_step("second step", deps=[s1.id])
        assert s1.id > 0
        assert s2.id > s1.id
        assert s2.deps == [s1.id]
        assert s1.status == PlanStatus.PENDING

        all_steps = await tmp_store.list_plan_steps()
        assert [s.description for s in all_steps] == ["first step", "second step"]
        assert all_steps[1].deps == [s1.id]

        pending = await tmp_store.list_plan_steps(status=PlanStatus.PENDING)
        assert len(pending) == 2

    async def test_mark_done_updates_status(self, tmp_store: ScratchpadStore) -> None:
        await tmp_store.init()
        s1 = await tmp_store.add_plan_step("task")
        updated = await tmp_store.mark_done(s1.id)
        assert updated is not None
        assert updated.status == PlanStatus.DONE
        assert updated.id == s1.id

        done = await tmp_store.list_plan_steps(status=PlanStatus.DONE)
        assert len(done) == 1
        assert done[0].id == s1.id
