"""End-to-end smoke tests for Phase 3 v1.2.0 Write context (Step 4).

These tests exercise the public API of the scratchpad subsystem in
combination: store + tool runtime + factory wiring + shared SQLite
file with CompactStore. They do NOT cover every code path (the
unit tests in test_scratchpad*.py do that) — they verify the
subsystem hangs together end-to-end.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.agents.compact_store import CompactStore, CompactRecord
from harness.agents.scratchpad import Note, NoteLevel, PlanStatus
from harness.agents.scratchpad_store import (
    DEFAULT_L0_MAX_BYTES,
    ScratchpadStore,
)
from harness.agents.runner import AgentRunner
from harness.server.agent.runtime import ToolRuntime


# === E2E: write + read round-trip ===

class TestE2EWriteRead:
    async def test_write_then_read_returns_note(self, tmp_path: Path) -> None:
        """The simplest end-to-end: write a note, read it back."""
        store = ScratchpadStore(
            tmp_path / "agent-jobs.db",
            session_id="e2e-1", agent_id="solomon",
        )
        await store.init()
        note = await store.write_note(
            NoteLevel.L1, "decision: use OAuth 2.0 PKCE",
            tags=["auth", "decision"],
        )
        assert note.id > 0

        notes = await store.read_notes(NoteLevel.L1)
        assert len(notes) == 1
        assert notes[0].id == note.id
        assert notes[0].content == "decision: use OAuth 2.0 PKCE"
        assert notes[0].tags == ["auth", "decision"]


# === E2E: plan lifecycle ===

class TestE2EPlanLifecycle:
    async def test_add_list_mark_done_full_cycle(self, tmp_path: Path) -> None:
        store = ScratchpadStore(
            tmp_path / "agent-jobs.db",
            session_id="e2e-plan", agent_id="solomon",
        )
        await store.init()
        s1 = await store.add_plan_step("draft spec")
        s2 = await store.add_plan_step("implement", deps=[s1.id])
        s3 = await store.add_plan_step("test", deps=[s2.id])

        # All three pending.
        pending = await store.list_plan_steps(status=PlanStatus.PENDING)
        assert len(pending) == 3
        assert [s.id for s in pending] == [s1.id, s2.id, s3.id]

        # Mark s1 done; pending count drops to 2.
        updated = await store.mark_done(s1.id)
        assert updated is not None
        assert updated.status == PlanStatus.DONE

        pending = await store.list_plan_steps(status=PlanStatus.PENDING)
        assert len(pending) == 2
        done = await store.list_plan_steps(status=PlanStatus.DONE)
        assert len(done) == 1
        assert done[0].id == s1.id


# === E2E: L0 cap with realistic text ===

class TestE2EL0CapRealistic:
    async def test_10_notes_of_200_bytes_caps_at_1kb(self, tmp_path: Path) -> None:
        """Default cap = 1024 bytes. 10 × 200-byte notes = 2000 bytes
        → auto-prune must keep total at ≤ 1024."""
        store = ScratchpadStore(
            tmp_path / "agent-jobs.db",
            session_id="e2e-l0", agent_id="solomon",
            l0_max_bytes=DEFAULT_L0_MAX_BYTES,
        )
        await store.init()
        # 10 × 200-byte notes = 2000 bytes total. With cap=1024, auto-prune
        # must keep total at ≤ 1024.
        for i in range(10):
            # Build a 200-byte note: 'f' * 200 — exact 200 bytes ASCII.
            content = "f" * 200
            assert len(content.encode("utf-8")) == 200
            await store.write_note(NoteLevel.L0, content)

        total = await store.l0_size_bytes()
        assert total <= 1024, f"L0 total {total} bytes exceeds cap 1024"

        kept = await store.read_notes(NoteLevel.L0)
        # Some notes pruned, at least one must survive.
        assert len(kept) < 10
        assert len(kept) >= 1


# === E2E: ToolRuntime → Store ===

class TestE2EToolRuntimeToStore:
    async def test_runtime_tool_call_persists_to_store(
        self, tmp_path: Path,
    ) -> None:
        """The LLM-facing tool path actually writes to the store."""
        store = ScratchpadStore(
            tmp_path / "agent-jobs.db",
            session_id="e2e-tool", agent_id="solomon",
        )
        await store.init()
        rt = ToolRuntime(project_root=tmp_path, scratchpad=store)

        res = await rt.execute("scratchpad_write_note", {
            "level": "L1", "content": "via runtime", "tags": ["e2e"],
        })
        assert res.ok is True, res.error

        # Verify the store has the note.
        notes = await store.read_notes(NoteLevel.L1)
        assert len(notes) == 1
        assert notes[0].content == "via runtime"
        assert notes[0].tags == ["e2e"]


# === E2E: shared DB with CompactStore ===

class TestE2ESharedDBWithCompactStore:
    async def test_scratchpad_and_compact_coexist_on_same_db(
        self, tmp_path: Path,
    ) -> None:
        """Both stores must coexist on the same ``agent-jobs.db``
        file with no schema collision (separate tables, separate indexes)."""
        db_path = tmp_path / "agent-jobs.db"

        # Open both stores against the same file.
        sp = ScratchpadStore(
            db_path, session_id="e2e-shared", agent_id="solomon",
        )
        cs = CompactStore(db_path)
        await sp.init()
        await cs.init()

        # Write to each.
        await sp.write_note(NoteLevel.L2, "scratchpad note")
        await cs.insert(CompactRecord(
            session_id="e2e-shared",
            version=0,  # overwritten by insert() with MAX+1
            source_hash="abc",
            original_tokens=100,
            compacted_tokens=30,
            original_message_count=5,
            kept_message_ids=[1, 2, 3],
            summary="compact summary",
            model="qwen3:8b",
            trigger_kind="auto_load_history",
            outcome="ok",
            created_at=1.0,
            duration_ms=10.0,
        ))

        # Read from each — both must work.
        notes = await sp.read_notes(NoteLevel.L2)
        compacts = await cs.list_for_session("e2e-shared")
        assert len(notes) == 1
        assert len(compacts) == 1
        assert notes[0].content == "scratchpad note"
        assert compacts[0].summary == "compact summary"

        # Schema introspection: both tables present, no migration collision.
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('scratchpad_notes', 'plan_steps', 'compact_store')",
            ) as cur:
                rows = await cur.fetchall()
        table_names = {r[0] for r in rows}
        assert table_names == {"scratchpad_notes", "plan_steps", "compact_store"}
