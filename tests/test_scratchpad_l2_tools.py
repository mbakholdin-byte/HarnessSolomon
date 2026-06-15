"""Tests for Phase 3 v1.3.0 L2 scratchpad tools (search + promote-to-L1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from harness.agents.scratchpad import Note, NoteLevel
from harness.server.agent.runtime import ToolRuntime


# === Helpers ===

class _FakeEmbedder:
    dim: int = 4

    async def embed_query(self, text: str) -> list[float]:
        return [0.0] * self.dim


class _FakeL2Retriever:
    """Records curated_search calls; returns scripted results."""

    def __init__(self, scripted: list[tuple[Note, float]] | None = None) -> None:
        self._scripted = scripted or []
        self.calls: list[dict[str, Any]] = []

    async def curated_search(
        self,
        query: str,
        top_k: int = 10,
        candidate_k: int = 50,
        *,
        notes: list[Note] | None = None,
        router: Any = None,
        model: str = "qwen3:8b",
    ) -> list[tuple[Note, float]]:
        self.calls.append(
            {
                "query": query, "top_k": top_k,
                "candidate_k": candidate_k, "model": model,
                "router": router, "notes_count": len(notes or []),
            }
        )
        return self._scripted


class _FakeRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def completion(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, "model": model})
        result = MagicMock()
        result.content = ""
        return result


class _FakeScratchpad:
    """Minimal scratchpad stub exposing the methods the runtime uses."""

    def __init__(self, l2_notes: list[Note] | None = None) -> None:
        self._l2_notes = l2_notes or []
        self._session_id = "s1"
        self.write_calls: list[dict[str, Any]] = []
        self._next_id = 100

    async def init(self) -> None: ...

    async def read_notes(
        self,
        level: str | NoteLevel | None = None,
        *,
        limit: int = 100,
    ) -> list[Note]:
        if level in (NoteLevel.L2, "L2"):
            return list(self._l2_notes)[:limit]
        return []

    async def write_note(
        self,
        level: str,
        content: str,
        tags: list[str] | None = None,
    ) -> Note:
        self.write_calls.append(
            {"level": level, "content": content, "tags": tags or []}
        )
        self._next_id += 1
        return Note(
            id=self._next_id, session_id=self._session_id,
            agent_id="a1", level=NoteLevel(level),
            content=content, tags=tags or [],
            created_at=99999.0,
        )


def _note(*, id: int, content: str, score: float = 80.0) -> Note:
    return Note(
        id=id, session_id="s1", agent_id="a1",
        level=NoteLevel.L2, content=content, tags=["l2"],
        created_at=12345.0 + id,
    )


# === scratchpad_l2_search ===

class TestL2SearchTool:
    async def test_search_returns_top_k_notes(
        self, tmp_path: Path,
    ) -> None:
        l2_notes = [_note(id=1, content="alpha"), _note(id=2, content="beta")]
        retriever = _FakeL2Retriever(
            scripted=[(l2_notes[0], 95.0), (l2_notes[1], 60.0)]
        )
        scratchpad = _FakeScratchpad(l2_notes=l2_notes)
        runtime = ToolRuntime(
            project_root=tmp_path,
            scratchpad=scratchpad,  # type: ignore[arg-type]
            l2_retriever=retriever,  # type: ignore[arg-type]
            l2_router=_FakeRouter(),  # type: ignore[arg-type]
        )
        result = await runtime.execute(
            "scratchpad_l2_search", {"query": "alpha", "top_k": 5},
        )
        assert result.ok
        payload = json.loads(result.output)
        assert payload["query"] == "alpha"
        assert payload["count"] == 2
        assert payload["results"][0]["id"] == 1
        assert payload["results"][0]["score"] == 95.0

    async def test_search_calls_curated_with_router(
        self, tmp_path: Path,
    ) -> None:
        retriever = _FakeL2Retriever()
        scratchpad = _FakeScratchpad(l2_notes=[_note(id=1, content="x")])
        router = _FakeRouter()
        runtime = ToolRuntime(
            project_root=tmp_path,
            scratchpad=scratchpad,  # type: ignore[arg-type]
            l2_retriever=retriever,  # type: ignore[arg-type]
            l2_router=router,  # type: ignore[arg-type]
            l2_curator_model="qwen3:8b",
        )
        await runtime.execute(
            "scratchpad_l2_search", {"query": "q"},
        )
        assert len(retriever.calls) == 1
        assert retriever.calls[0]["router"] is router
        assert retriever.calls[0]["model"] == "qwen3:8b"

    async def test_search_no_retriever_returns_error(
        self, tmp_path: Path,
    ) -> None:
        runtime = ToolRuntime(project_root=tmp_path)
        result = await runtime.execute(
            "scratchpad_l2_search", {"query": "q"},
        )
        assert not result.ok
        assert "L2 retriever not enabled" in result.error

    async def test_search_empty_query_returns_error(
        self, tmp_path: Path,
    ) -> None:
        retriever = _FakeL2Retriever()
        scratchpad = _FakeScratchpad()
        runtime = ToolRuntime(
            project_root=tmp_path,
            scratchpad=scratchpad,  # type: ignore[arg-type]
            l2_retriever=retriever,  # type: ignore[arg-type]
        )
        result = await runtime.execute(
            "scratchpad_l2_search", {"query": ""},
        )
        assert not result.ok
        assert "query" in result.error

    async def test_search_top_k_clamped(self, tmp_path: Path) -> None:
        retriever = _FakeL2Retriever()
        scratchpad = _FakeScratchpad()
        runtime = ToolRuntime(
            project_root=tmp_path,
            scratchpad=scratchpad,  # type: ignore[arg-type]
            l2_retriever=retriever,  # type: ignore[arg-type]
        )
        # top_k=999 should be clamped to 50.
        await runtime.execute(
            "scratchpad_l2_search", {"query": "q", "top_k": 999},
        )
        assert retriever.calls[0]["top_k"] == 50


# === scratchpad_l2_promote_to_l1 ===

class TestL2PromoteTool:
    async def test_promote_writes_l1_note(self, tmp_path: Path) -> None:
        l2_notes = [
            _note(id=1, content="alpha fact"),
            _note(id=2, content="beta fact"),
        ]
        retriever = _FakeL2Retriever(
            scripted=[(l2_notes[0], 85.0), (l2_notes[1], 70.0)]
        )
        scratchpad = _FakeScratchpad()
        runtime = ToolRuntime(
            project_root=tmp_path,
            scratchpad=scratchpad,  # type: ignore[arg-type]
            l2_retriever=retriever,  # type: ignore[arg-type]
            l2_router=_FakeRouter(),  # type: ignore[arg-type]
        )
        result = await runtime.execute(
            "scratchpad_l2_promote_to_l1",
            {"query": "alpha", "max_notes": 5},
        )
        assert result.ok
        payload = json.loads(result.output)
        assert payload["status"] == "promoted"
        assert payload["query"] == "alpha"
        assert 1 in payload["source_note_ids"]
        assert 2 in payload["source_note_ids"]
        # An L1 note was written.
        assert len(scratchpad.write_calls) == 1
        write = scratchpad.write_calls[0]
        assert write["level"] == "L1"
        assert "alpha" in write["content"]
        assert "l2-summary" in write["tags"]

    async def test_promote_below_threshold_skips(
        self, tmp_path: Path,
    ) -> None:
        # All candidates score < 50 → no L1 write.
        l2_notes = [
            _note(id=1, content="x"),
            _note(id=2, content="y"),
        ]
        retriever = _FakeL2Retriever(
            scripted=[(l2_notes[0], 10.0), (l2_notes[1], 20.0)]
        )
        scratchpad = _FakeScratchpad()
        runtime = ToolRuntime(
            project_root=tmp_path,
            scratchpad=scratchpad,  # type: ignore[arg-type]
            l2_retriever=retriever,  # type: ignore[arg-type]
        )
        result = await runtime.execute(
            "scratchpad_l2_promote_to_l1", {"query": "x"},
        )
        assert result.ok
        payload = json.loads(result.output)
        assert payload["status"] == "below_threshold"
        assert len(scratchpad.write_calls) == 0

    async def test_promote_no_candidates(self, tmp_path: Path) -> None:
        retriever = _FakeL2Retriever(scripted=[])
        scratchpad = _FakeScratchpad()
        runtime = ToolRuntime(
            project_root=tmp_path,
            scratchpad=scratchpad,  # type: ignore[arg-type]
            l2_retriever=retriever,  # type: ignore[arg-type]
        )
        result = await runtime.execute(
            "scratchpad_l2_promote_to_l1", {"query": "x"},
        )
        assert result.ok
        payload = json.loads(result.output)
        assert payload["status"] == "no_candidates"

    async def test_promote_no_retriever_returns_error(
        self, tmp_path: Path,
    ) -> None:
        runtime = ToolRuntime(project_root=tmp_path)
        result = await runtime.execute(
            "scratchpad_l2_promote_to_l1", {"query": "x"},
        )
        assert not result.ok
        assert "L2 retriever not enabled" in result.error
