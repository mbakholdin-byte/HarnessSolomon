"""Tests for Phase 3 v1.3.0 LLM-curator re-ranking of L2 retrieval."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.agents.l2_retriever import (
    L2Retriever,
    _build_curator_prompt,
    parse_curator_response,
)
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
    dim: int = 4

    async def embed_query(self, text: str) -> list[float]:
        return [0.0] * self.dim


class _FakeRouter:
    """Stub LLM router — records calls and returns scripted content."""

    def __init__(self, content: str = "", raise_exc: Exception | None = None) -> None:
        self._content = content
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def completion(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, "model": model, "tools": tools})
        if self._raise is not None:
            raise self._raise
        result = MagicMock()
        result.content = self._content
        return result


# === _build_curator_prompt ===

class TestCuratorPrompt:
    def test_prompt_includes_query_and_note_ids(self) -> None:
        notes = [_note(id=42, content="alpha"), _note(id=43, content="beta")]
        prompt = _build_curator_prompt("what is X?", notes)
        assert "what is X?" in prompt
        assert "id=42" in prompt
        assert "id=43" in prompt
        assert "alpha" in prompt
        assert "beta" in prompt

    def test_prompt_includes_tags(self) -> None:
        notes = [_note(id=1, content="x", tags=["important", "from-user"])]
        prompt = _build_curator_prompt("query", notes)
        assert "[important,from-user]" in prompt

    def test_prompt_truncates_long_content(self) -> None:
        notes = [_note(id=1, content="x" * 1000)]
        prompt = _build_curator_prompt("q", notes)
        # 500-char cap is enforced; 1000-char input is cut.
        assert "x" * 500 in prompt
        assert "x" * 501 not in prompt


# === parse_curator_response ===

class TestCuratorResponseParse:
    def test_valid_json_parsed(self) -> None:
        notes = [_note(id=1, content="a"), _note(id=2, content="b")]
        response = '[{"id": 1, "score": 85.5}, {"id": 2, "score": 30}]'
        result = parse_curator_response(response, notes)
        assert len(result) == 2
        assert int(result[0][0].id) == 1
        assert result[0][1] == 85.5
        assert int(result[1][0].id) == 2

    def test_markdown_fenced_json_parsed(self) -> None:
        notes = [_note(id=1, content="a")]
        response = '```json\n[{"id": 1, "score": 90}]\n```'
        result = parse_curator_response(response, notes)
        assert len(result) == 1
        assert result[0][1] == 90.0

    def test_preamble_prose_around_json_parsed(self) -> None:
        notes = [_note(id=1, content="a")]
        response = 'Sure! Here is the result:\n[{"id": 1, "score": 42}]\nHope that helps.'
        result = parse_curator_response(response, notes)
        assert len(result) == 1
        assert result[0][1] == 42.0

    def test_missing_field_skipped(self) -> None:
        notes = [_note(id=1, content="a"), _note(id=2, content="b")]
        response = '[{"id": 1}, {"id": 2, "score": 50}]'
        result = parse_curator_response(response, notes)
        # id=1 missing score → skipped; id=2 valid.
        assert len(result) == 1
        assert int(result[0][0].id) == 2

    def test_unknown_id_skipped(self) -> None:
        notes = [_note(id=1, content="a")]
        response = '[{"id": 99, "score": 80}, {"id": 1, "score": 50}]'
        result = parse_curator_response(response, notes)
        assert len(result) == 1
        assert int(result[0][0].id) == 1

    def test_malformed_json_returns_empty(self) -> None:
        notes = [_note(id=1, content="a")]
        result = parse_curator_response("not json at all", notes)
        assert result == []

    def test_empty_response_returns_empty(self) -> None:
        assert parse_curator_response("", [_note(id=1, content="a")]) == []

    def test_score_out_of_range_skipped(self) -> None:
        notes = [_note(id=1, content="a"), _note(id=2, content="b")]
        response = '[{"id": 1, "score": 150}, {"id": 2, "score": -5}]'
        result = parse_curator_response(response, notes)
        assert result == []


# === curated_search ===

class TestCuratedSearch:
    async def test_curated_search_ranks_via_llm(
        self, tmp_path: Path,
    ) -> None:
        # Use a query that shares tokens with at least one note so
        # the BM25 path produces candidates; the curator then re-ranks.
        notes = [
            _note(id=1, content="alpha"),
            _note(id=2, content="beta alpha omega"),   # matches "alpha"
            _note(id=3, content="gamma"),
        ]
        db = tmp_path / "l2.db"
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS scratchpad_notes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "session_id TEXT NOT NULL,"
                "agent_id TEXT,"
                "level TEXT NOT NULL,"
                "content TEXT NOT NULL,"
                "tags TEXT NOT NULL,"
                "created_at REAL NOT NULL)"
            )
            for n in notes:
                await conn.execute(
                    "INSERT OR REPLACE INTO scratchpad_notes "
                    "(id, session_id, agent_id, level, content, tags, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (n.id, "s1", "a1", "L2", n.content, "[]", 12345.0),
                )
            await conn.commit()
        l2_vec = SqliteL2Store(db)
        # Curator says id=2 is the best match.
        router = _FakeRouter(
            content='[{"id": 2, "score": 95}, {"id": 1, "score": 30}]'
        )
        retriever = L2Retriever(l2_vec, _FakeEmbedder())
        hits = await retriever.curated_search(
            "alpha", top_k=2, candidate_k=10, notes=notes,
            router=router, model="qwen3:8b",
        )
        assert len(hits) == 2
        assert int(hits[0][0].id) == 2
        assert hits[0][1] == 95.0
        assert int(hits[1][0].id) == 1
        # Router was called with the curator prompt.
        assert router.calls, "router should have been called"
        msgs = router.calls[0]["messages"]
        assert any("id=2" in m["content"] for m in msgs if m["role"] == "user")

    async def test_curated_search_no_router_falls_back_to_hybrid(
        self, tmp_path: Path,
    ) -> None:
        notes = [_note(id=1, content="alpha"), _note(id=2, content="beta")]
        db = tmp_path / "l2.db"
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS scratchpad_notes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "session_id TEXT NOT NULL,"
                "agent_id TEXT,"
                "level TEXT NOT NULL,"
                "content TEXT NOT NULL,"
                "tags TEXT NOT NULL,"
                "created_at REAL NOT NULL)"
            )
            for n in notes:
                await conn.execute(
                    "INSERT OR REPLACE INTO scratchpad_notes "
                    "(id, session_id, agent_id, level, content, tags, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (n.id, "s1", "a1", "L2", n.content, "[]", 12345.0),
                )
            await conn.commit()
        l2_vec = SqliteL2Store(db)
        retriever = L2Retriever(l2_vec, _FakeEmbedder())
        hits = await retriever.curated_search(
            "alpha", top_k=5, notes=notes, router=None,
        )
        # Without a router we return plain hybrid top-K. The exact
        # ranking depends on BM25+dense; we just assert we get a
        # list and id=1 is in it (BM25 match).
        assert isinstance(hits, list)
        ids = [int(n.id) for n, _ in hits]
        assert 1 in ids

    async def test_curated_search_router_raises_falls_back(
        self, tmp_path: Path,
    ) -> None:
        notes = [_note(id=1, content="alpha"), _note(id=2, content="beta")]
        db = tmp_path / "l2.db"
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS scratchpad_notes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "session_id TEXT NOT NULL,"
                "agent_id TEXT,"
                "level TEXT NOT NULL,"
                "content TEXT NOT NULL,"
                "tags TEXT NOT NULL,"
                "created_at REAL NOT NULL)"
            )
            for n in notes:
                await conn.execute(
                    "INSERT OR REPLACE INTO scratchpad_notes "
                    "(id, session_id, agent_id, level, content, tags, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (n.id, "s1", "a1", "L2", n.content, "[]", 12345.0),
                )
            await conn.commit()
        l2_vec = SqliteL2Store(db)
        router = _FakeRouter(raise_exc=RuntimeError("LLM unavailable"))
        retriever = L2Retriever(l2_vec, _FakeEmbedder())
        hits = await retriever.curated_search(
            "alpha", top_k=5, notes=notes, router=router,
        )
        # LLM failure → fall back to plain hybrid. The result is a
        # list and the chat loop is intact (no exception escaped).
        assert isinstance(hits, list)

    async def test_curated_search_empty_corpus(self) -> None:
        retriever = L2Retriever(_FakeEmbedder(), _FakeEmbedder())  # type: ignore[arg-type]
        router = _FakeRouter(content="[]")
        hits = await retriever.curated_search(
            "anything", top_k=5, notes=[], router=router,
        )
        assert hits == []
