"""Tests for :mod:`harness.agents.compact_store` (Phase 3.5, Step 0)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from harness.agents.compact_store import (
    OUTCOMES,
    TRIGGER_KINDS,
    CompactRecord,
    CompactStore,
)


# === Fixtures ===

@pytest.fixture
def tmp_store(tmp_path: Path) -> CompactStore:
    """Return a fresh, initialised ``CompactStore`` in a temp dir."""
    store = CompactStore(tmp_path / "agent-jobs.db")
    return store


def _make_record(
    session_id: str = "sess-1",
    source_hash: str = "abc123",
    kept_ids: list[int] | None = None,
    outcome: str = "ok",
    summary: str = "A compact summary of the dropped region.",
) -> CompactRecord:
    """Helper: build a minimal valid record for tests."""
    return CompactRecord(
        session_id=session_id,
        version=0,  # overwritten by insert()
        source_hash=source_hash,
        original_tokens=1200,
        compacted_tokens=300,
        original_message_count=20,
        kept_message_ids=kept_ids if kept_ids is not None else [1, 2, 3, 4, 5, 6],
        summary=summary,
        model="qwen3:8b",
        trigger_kind=TRIGGER_KINDS[0],
        outcome=outcome,
        created_at=time.time(),
        duration_ms=123.4,
    )


# === Schema migration ===

class TestInit:
    async def test_init_creates_table(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        # After init, the file should exist and the table is queryable.
        assert tmp_store._db_path.exists()
        assert await tmp_store.count() == 0

    async def test_init_idempotent(self, tmp_store: CompactStore) -> None:
        # Two init calls in a row should not error and should not
        # duplicate the table.
        await tmp_store.init()
        await tmp_store.init()
        assert await tmp_store.count() == 0

    async def test_init_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "agent-jobs.db"
        store = CompactStore(nested)
        await store.init()
        assert nested.parent.is_dir()
        assert nested.exists()

    async def test_init_creates_index_on_session_recent(
        self, tmp_store: CompactStore,
    ) -> None:
        await tmp_store.init()
        async with __import__("aiosqlite").connect(tmp_store._db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_compact_store_session_recent'",
            ) as cur:
                row = await cur.fetchone()
        assert row is not None


# === Insert + auto-version ===

class TestInsert:
    async def test_insert_assigns_version_one(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        rec = _make_record()
        v = await tmp_store.insert(rec)
        assert v == 1
        assert rec.version == 1
        assert await tmp_store.count() == 1

    async def test_insert_increments_version_per_session(
        self, tmp_store: CompactStore,
    ) -> None:
        await tmp_store.init()
        v1 = await tmp_store.insert(_make_record(source_hash="h1"))
        v2 = await tmp_store.insert(_make_record(source_hash="h2"))
        v3 = await tmp_store.insert(_make_record(source_hash="h3"))
        assert v1 == 1
        assert v2 == 2
        assert v3 == 3

    async def test_insert_isolates_versions_across_sessions(
        self, tmp_store: CompactStore,
    ) -> None:
        await tmp_store.init()
        v_a = await tmp_store.insert(_make_record(session_id="A", source_hash="h"))
        v_b = await tmp_store.insert(_make_record(session_id="B", source_hash="h"))
        v_a2 = await tmp_store.insert(_make_record(session_id="A", source_hash="h2"))
        # Each session gets its own monotonic counter.
        assert v_a == 1
        assert v_b == 1
        assert v_a2 == 2

    async def test_insert_persists_kept_ids_as_json(
        self, tmp_store: CompactStore,
    ) -> None:
        await tmp_store.init()
        kept = [10, 11, 12, 13, 14, 15]
        await tmp_store.insert(_make_record(kept_ids=kept))
        recs = await tmp_store.list_for_session("sess-1")
        assert recs[0].kept_message_ids == kept

    async def test_insert_duplicate_version_raises(
        self, tmp_store: CompactStore,
    ) -> None:
        await tmp_store.init()
        await tmp_store.insert(_make_record())
        # Force a version collision by manually setting the same version.
        rec = _make_record()
        rec.version = 1
        import aiosqlite
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            async with aiosqlite.connect(tmp_store._db_path) as db:
                await db.execute(
                    "INSERT INTO compact_store ("
                    "  session_id, version, source_hash, original_tokens, "
                    "  compacted_tokens, original_message_count, "
                    "  kept_message_ids, summary, model, trigger_kind, "
                    "  outcome, created_at, duration_ms"
                    ") VALUES ("
                    "  'sess-1', 1, 'h2', 0, 0, 0, '[]', '', '', '', 'ok', 0, 0"
                    ")",
                )
                await db.commit()


# === Lookup ===

class TestLookupCached:
    async def test_lookup_miss_returns_none(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        result = await tmp_store.lookup_cached("sess-1", "nothere")
        assert result is None

    async def test_lookup_hit_returns_latest_version(
        self, tmp_store: CompactStore,
    ) -> None:
        await tmp_store.init()
        await tmp_store.insert(_make_record(source_hash="h1"))
        await tmp_store.insert(_make_record(source_hash="h1", summary="v2"))
        await tmp_store.insert(_make_record(source_hash="h1", summary="v3"))
        rec = await tmp_store.lookup_cached("sess-1", "h1")
        assert rec is not None
        assert rec.version == 3
        assert rec.summary == "v3"

    async def test_lookup_filters_by_source_hash(
        self, tmp_store: CompactStore,
    ) -> None:
        await tmp_store.init()
        await tmp_store.insert(_make_record(source_hash="h1"))
        await tmp_store.insert(_make_record(source_hash="h2"))
        # Looking up h1 returns the v1 record.
        rec1 = await tmp_store.lookup_cached("sess-1", "h1")
        assert rec1 is not None
        assert rec1.version == 1
        # Looking up h2 returns the v2 record.
        rec2 = await tmp_store.lookup_cached("sess-1", "h2")
        assert rec2 is not None
        assert rec2.version == 2

    async def test_lookup_isolates_by_session(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        await tmp_store.insert(_make_record(session_id="A", source_hash="h"))
        await tmp_store.insert(_make_record(session_id="B", source_hash="h"))
        rec_a = await tmp_store.lookup_cached("A", "h")
        rec_b = await tmp_store.lookup_cached("B", "h")
        assert rec_a is not None
        assert rec_b is not None
        # Same hash but different session → both still hit (versions 1 each).
        assert rec_a.session_id == "A"
        assert rec_b.session_id == "B"


# === list_for_session ===

class TestListForSession:
    async def test_list_empty(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        assert await tmp_store.list_for_session("none") == []

    async def test_list_newest_first(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        for i in range(5):
            await tmp_store.insert(_make_record(source_hash=f"h{i}"))
        recs = await tmp_store.list_for_session("sess-1")
        assert [r.version for r in recs] == [5, 4, 3, 2, 1]

    async def test_list_limit(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        for i in range(10):
            await tmp_store.insert(_make_record(source_hash=f"h{i}"))
        recs = await tmp_store.list_for_session("sess-1", limit=3)
        assert len(recs) == 3
        assert [r.version for r in recs] == [10, 9, 8]

    async def test_list_isolates_session(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        await tmp_store.insert(_make_record(session_id="A"))
        await tmp_store.insert(_make_record(session_id="B"))
        await tmp_store.insert(_make_record(session_id="B"))
        assert len(await tmp_store.list_for_session("A")) == 1
        assert len(await tmp_store.list_for_session("B")) == 2


# === count ===

class TestCount:
    async def test_count_empty(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        assert await tmp_store.count() == 0

    async def test_count_grows_with_inserts(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        for i in range(7):
            await tmp_store.insert(_make_record(source_hash=f"h{i}"))
        assert await tmp_store.count() == 7

    async def test_count_across_sessions(self, tmp_store: CompactStore) -> None:
        await tmp_store.init()
        await tmp_store.insert(_make_record(session_id="A"))
        await tmp_store.insert(_make_record(session_id="B"))
        await tmp_store.insert(_make_record(session_id="C"))
        assert await tmp_store.count() == 3


# === CompactRecord dataclass ===

class TestCompactRecord:
    def test_to_row_round_trip(self) -> None:
        rec = _make_record(kept_ids=[42, 43])
        row = rec.to_row()
        assert row["session_id"] == "sess-1"
        assert row["version"] == 0
        assert row["kept_message_ids"] == json.dumps([42, 43])

    def test_from_row_decodes_kept_ids(self) -> None:
        rec = _make_record(kept_ids=[100, 200])
        row = rec.to_row()
        # Simulate what aiosqlite returns: a dict-like with stringified JSON.
        round_tripped = CompactRecord.from_row(row)
        assert round_tripped.kept_message_ids == [100, 200]

    def test_from_row_with_list_kept_ids(self) -> None:
        # Defensive: if a caller pre-decodes the JSON, we should accept
        # a list too (matches the ``isinstance(kept, str)`` branch).
        rec = _make_record(kept_ids=[1, 2, 3])
        row = rec.to_row()
        row["kept_message_ids"] = [1, 2, 3]  # pre-decoded
        rt = CompactRecord.from_row(row)
        assert rt.kept_message_ids == [1, 2, 3]


# === Constants ===

class TestConstants:
    def test_outcomes_includes_ok(self) -> None:
        assert "ok" in OUTCOMES

    def test_trigger_kinds_includes_auto_load_history(self) -> None:
        assert "auto_load_history" in TRIGGER_KINDS
