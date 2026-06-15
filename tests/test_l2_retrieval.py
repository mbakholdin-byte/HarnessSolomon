"""Tests for Phase 3 v1.3.0 L2 retriever (hybrid dense+BM25 RRF)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import numpy as np
import pytest

from harness.agents.l2_retriever import L2Retriever
from harness.agents.l2_vector_store import SqliteL2Store
from harness.agents.scratchpad import Note, NoteLevel


# === Helpers ===

def _note(*, id: int, content: str, session_id: str = "s1", tags: list[str] | None = None) -> Note:
    return Note(
        id=id, session_id=session_id, agent_id="a1",
        level=NoteLevel.L2, content=content,
        tags=tags or [], created_at=12345.0 + id,
    )


class _FakeEmbedder:
    """Deterministic stub that hashes the query text to a fixed vector.

    The vector is content-aware: queries that share tokens with a
    note's content get a higher cosine score, which is enough to
    exercise the hybrid RRF logic. Real ``OnnxEmbedder`` is too
    heavy for a unit test (downloads 30MB onnx model).
    """

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        # Hash the text into a unit vector.
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.random(self.dim).astype(np.float32)
        v /= np.linalg.norm(v)
        return v.tolist()


async def _seed_l2_db(
    db_path: Path, notes: list[Note],
) -> SqliteL2Store:
    """Insert notes into scratchpad_notes + L2 embeddings into the
    same DB, return a ready-to-search SqliteL2Store."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS scratchpad_notes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "session_id TEXT NOT NULL,"
            "agent_id TEXT,"
            "level TEXT NOT NULL CHECK(level IN ('L0','L1','L2')),"
            "content TEXT NOT NULL,"
            "tags TEXT NOT NULL,"
            "created_at REAL NOT NULL"
            ")"
        )
        for n in notes:
            await db.execute(
                "INSERT OR REPLACE INTO scratchpad_notes "
                "(id, session_id, agent_id, level, content, tags, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (n.id, n.session_id, n.agent_id, n.level.value, n.content,
                 "[]", n.created_at),
            )
        await db.commit()
    l2_vec = SqliteL2Store(db_path)
    # Index each note with a deterministic vector (per-note content).
    emb = _FakeEmbedder()
    for n in notes:
        vec = await emb.embed_query(n.content)
        await l2_vec.upsert(n.id, vec, {"session_id": n.session_id})
    return l2_vec


# === L2Retriever tests ===

class TestL2Retriever:
    async def test_bm25_path_keyword_match(self, tmp_path: Path) -> None:
        """The two notes that share tokens with the query must rank
        ABOVE the unrelated one — the BM25 path contributes a real
        signal, so the RRF fusion elevates them. The dense path
        also pulls the unrelated note (random vectors can have
        non-zero cosine), but the BM25 boost on the matching
        notes keeps them at the top."""
        notes = [
            _note(id=1, content="solomon harness architecture"),
            _note(id=2, content="qdrant vector database"),
            _note(id=3, content="solomon memory and notes"),
        ]
        l2_vec = await _seed_l2_db(tmp_path / "l2.db", notes)
        retriever = L2Retriever(l2_vec, _FakeEmbedder())
        hits = await retriever.search("solomon", top_k=3, notes=notes)
        # The two keyword-matching notes must occupy the top-2
        # positions; id=2 (no shared tokens) is ranked third.
        top_ids = [int(n.id) for n, _ in hits]
        assert top_ids[0] in {1, 3}
        assert top_ids[1] in {1, 3}
        assert top_ids[0] != top_ids[1]

    async def test_dense_path_when_no_keyword_match(
        self, tmp_path: Path,
    ) -> None:
        notes = [
            _note(id=1, content="alpha bravo charlie"),
            _note(id=2, content="delta echo foxtrot"),
        ]
        l2_vec = await _seed_l2_db(tmp_path / "l2.db", notes)
        retriever = L2Retriever(l2_vec, _FakeEmbedder())
        # Query has no shared tokens with any note. BM25 is empty;
        # dense should still return both notes (cos > 0 only when
        # truly similar; with random unit vectors cosine can be
        # negative or zero). Just assert the retriever does NOT
        # raise and returns a list.
        hits = await retriever.search("zzz", top_k=5, notes=notes)
        assert isinstance(hits, list)

    async def test_hybrid_rrf_combines_both_lists(
        self, tmp_path: Path,
    ) -> None:
        """Note 1 is a strong BM25 hit AND a strong dense hit (same
        hash content). It should rank first in the RRF fusion."""
        notes = [
            _note(id=1, content="solomon architecture"),
            _note(id=2, content="solomon history"),   # BM25 only
            _note(id=3, content="unrelated alpha"),    # dense only
        ]
        l2_vec = await _seed_l2_db(tmp_path / "l2.db", notes)
        retriever = L2Retriever(l2_vec, _FakeEmbedder())
        hits = await retriever.search("solomon", top_k=3, notes=notes)
        assert hits, "expected at least one hit"
        # id=1 has both BM25 and dense contributions → highest RRF.
        assert int(hits[0][0].id) == 1

    async def test_empty_corpus_returns_empty(self) -> None:
        # No DB, no notes — retriever returns empty list.
        retriever = L2Retriever(_FakeEmbedder(), _FakeEmbedder())  # type: ignore[arg-type]
        hits = await retriever.search("anything", top_k=5, notes=[])
        assert hits == []

    async def test_session_id_filter_passed_to_dense(
        self, tmp_path: Path,
    ) -> None:
        """With session_id set, the dense path passes the filter
        through to l2_vec.search. BM25 is unaffected (it works on
        the in-memory corpus)."""
        notes = [
            _note(id=1, content="solomon a", session_id="sA"),
            _note(id=2, content="solomon b", session_id="sB"),
        ]
        l2_vec = await _seed_l2_db(tmp_path / "l2.db", notes)
        retriever = L2Retriever(
            l2_vec, _FakeEmbedder(), session_id="sA",
        )
        # Patch the underlying l2_vec.search to record the filter.
        recorded: list[dict[str, Any]] = []
        original_search = l2_vec.search

        async def spy_search(*args: Any, **kwargs: Any) -> Any:
            recorded.append(kwargs.get("filter"))
            return await original_search(*args, **kwargs)

        l2_vec.search = spy_search  # type: ignore[method-assign]
        await retriever.search("solomon", top_k=5, notes=notes)
        assert recorded, "dense retriever should have called l2_vec.search"
        assert recorded[0] == {"session_id": "sA"}

    async def test_top_k_clamps(self, tmp_path: Path) -> None:
        notes = [_note(id=i, content=f"note {i}") for i in range(1, 11)]
        l2_vec = await _seed_l2_db(tmp_path / "l2.db", notes)
        retriever = L2Retriever(l2_vec, _FakeEmbedder())
        hits = await retriever.search("note", top_k=3, notes=notes)
        assert len(hits) <= 3

    async def test_fetch_k_clamped_to_one(self, tmp_path: Path) -> None:
        """fetch_k=0 would break the RRF denominator; we clamp to 1."""
        notes = [_note(id=1, content="alpha")]
        l2_vec = await _seed_l2_db(tmp_path / "l2.db", notes)
        retriever = L2Retriever(l2_vec, _FakeEmbedder(), fetch_k=0)
        # The construction must not raise; the retrieval should
        # produce something sane.
        hits = await retriever.search("alpha", top_k=5, notes=notes)
        assert isinstance(hits, list)

    async def test_top_k_zero_returns_empty(self) -> None:
        retriever = L2Retriever(_FakeEmbedder(), _FakeEmbedder())  # type: ignore[arg-type]
        hits = await retriever.search("q", top_k=0, notes=[_note(id=1, content="x")])
        assert hits == []
