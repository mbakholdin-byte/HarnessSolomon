"""Tests for Phase 3 v1.3.1 offload recovery tools
(scratchpad_read_offloaded + scratchpad_search_offloaded)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.agents.scratchpad import Note, NoteLevel
from harness.agents.scratchpad_store import ScratchpadStore
from harness.config import Settings
from harness.server.agent.runtime import ToolRuntime


# === Helpers ===

class _FakeOffloader:
    """Minimal ToolOffloader double — read returns scripted content."""

    def __init__(
        self,
        *,
        notes_by_id: dict[int, Note] | None = None,
        read_result: str | None = "stub content",
    ) -> None:
        self._notes_by_id = notes_by_id or {}
        self._read_result = read_result
        self._settings = Settings()
        self.read_calls: list[dict[str, Any]] = []

    async def read(
        self, note_id: int, *, max_bytes: int = 4096,
    ) -> str | None:
        self.read_calls.append({"note_id": note_id, "max_bytes": max_bytes})
        if self._read_result is not None:
            return self._read_result
        return self._notes_by_id.get(note_id, None) and (
            self._notes_by_id[note_id].content[:max_bytes]
        )


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


# === Fixtures ===

@pytest.fixture
def tmp_store(tmp_path: Path) -> ScratchpadStore:
    return ScratchpadStore(
        tmp_path / "agent-jobs.db",
        session_id="sess-offload",
        agent_id="solomon",
    )


# === scratchpad_read_offloaded ===

class TestReadOffloadedTool:
    async def test_returns_truncated_content(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        offloader = _FakeOffloader(read_result="hello world")
        runtime = ToolRuntime(
            tmp_path,
            scratchpad=tmp_store,
            tool_offloader=offloader,
        )
        result = await runtime.execute(
            "scratchpad_read_offloaded", {"id": 42},
        )
        assert result.ok
        assert result.output == "hello world"
        assert offloader.read_calls[0]["note_id"] == 42

    async def test_uses_max_bytes_arg(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        offloader = _FakeOffloader(read_result="content")
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store, tool_offloader=offloader,
        )
        result = await runtime.execute(
            "scratchpad_read_offloaded", {"id": 1, "max_bytes": 100},
        )
        assert result.ok
        assert offloader.read_calls[0]["max_bytes"] == 100

    async def test_missing_note_returns_error(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        # offloader.read returns None on miss.
        offloader = _FakeOffloader(read_result=None)
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store, tool_offloader=offloader,
        )
        result = await runtime.execute(
            "scratchpad_read_offloaded", {"id": 999},
        )
        assert not result.ok
        assert "not found" in result.error

    async def test_no_offloader_returns_error(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store, tool_offloader=None,
        )
        result = await runtime.execute(
            "scratchpad_read_offloaded", {"id": 1},
        )
        assert not result.ok
        assert "offloader not enabled" in result.error

    async def test_invalid_id_returns_error(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        offloader = _FakeOffloader()
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store, tool_offloader=offloader,
        )
        # Negative id
        result = await runtime.execute(
            "scratchpad_read_offloaded", {"id": -1},
        )
        assert not result.ok
        assert "positive integer" in result.error
        # Zero id
        result = await runtime.execute(
            "scratchpad_read_offloaded", {"id": 0},
        )
        assert not result.ok
        # Non-int id
        result = await runtime.execute(
            "scratchpad_read_offloaded", {"id": "not-an-int"},
        )
        assert not result.ok


# === scratchpad_search_offloaded ===

class TestSearchOffloadedTool:
    async def test_returns_json_list(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        # Seed two offload-tagged L2 notes in the real store.
        n1 = await tmp_store.write_note(
            NoteLevel.L2, "alpha", tags=["#tool-offload", "#tool/bash"],
        )
        n2 = await tmp_store.write_note(
            NoteLevel.L2, "beta", tags=["#tool-offload", "#tool/grep"],
        )
        retriever = _FakeL2Retriever(
            [(n1, 95.0), (n2, 60.0)],
        )
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store,
            l2_retriever=retriever, l2_router=None,
        )
        result = await runtime.execute(
            "scratchpad_search_offloaded", {"query": "test", "top_k": 5},
        )
        assert result.ok
        payload = json.loads(result.output)
        assert len(payload) == 2
        assert payload[0]["id"] == n1.id
        assert payload[0]["score"] == 95.0
        assert payload[0]["preview"] == "alpha"
        assert "#tool-offload" in payload[0]["tags"]

    async def test_filters_l2_by_offload_tag(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        """The tool must pre-filter the L2 corpus to #tool-offload
        notes only — other L2 notes (regular scratchpad archive)
        must not be passed to the retriever."""
        await tmp_store.init()
        # Seed two L2 notes: one offload-tagged, one not.
        await tmp_store.write_note(
            NoteLevel.L2, "offloaded content",
            tags=["#tool-offload", "#tool/bash"],
        )
        await tmp_store.write_note(
            NoteLevel.L2, "regular L2 archive",
            tags=["archive"],
        )
        retriever = _FakeL2Retriever()
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store,
            l2_retriever=retriever, l2_router=None,
        )
        result = await runtime.execute(
            "scratchpad_search_offloaded", {"query": "test"},
        )
        assert result.ok
        # The retriever must have received only the #tool-offload note.
        assert retriever.calls[0]["notes_count"] == 1

    async def test_top_k_clamping(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        retriever = _FakeL2Retriever()
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store,
            l2_retriever=retriever, l2_router=None,
        )
        # top_k out of range.
        for bad_top_k in (0, 51, 100, -1):
            result = await runtime.execute(
                "scratchpad_search_offloaded",
                {"query": "test", "top_k": bad_top_k},
            )
            assert not result.ok
            assert "top_k" in result.error

    async def test_empty_offload_corpus_returns_empty_list(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        """No offload-tagged notes → empty JSON list, not an error."""
        await tmp_store.init()
        # Write a non-offload L2 note (must not be matched).
        await tmp_store.write_note(
            NoteLevel.L2, "regular", tags=["archive"],
        )
        retriever = _FakeL2Retriever()
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store,
            l2_retriever=retriever, l2_router=None,
        )
        result = await runtime.execute(
            "scratchpad_search_offloaded", {"query": "test"},
        )
        assert result.ok
        assert result.output == "[]"
        # The retriever must NOT have been called (no candidates).
        assert not retriever.calls

    async def test_no_l2_retriever_returns_error(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store, l2_retriever=None,
        )
        result = await runtime.execute(
            "scratchpad_search_offloaded", {"query": "test"},
        )
        assert not result.ok
        assert "L2 retriever not enabled" in result.error

    async def test_empty_query_returns_error(
        self, tmp_path: Path, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        retriever = _FakeL2Retriever()
        runtime = ToolRuntime(
            tmp_path, scratchpad=tmp_store,
            l2_retriever=retriever, l2_router=None,
        )
        result = await runtime.execute(
            "scratchpad_search_offloaded", {"query": ""},
        )
        assert not result.ok
        assert "query" in result.error


# === Tool schemas ===

class TestToolSchemas:
    def test_fourteen_tools_in_registry(self) -> None:
        from harness.server.agent.tools import TOOL_SCHEMAS
        names = {t["name"] for t in TOOL_SCHEMAS}
        assert "scratchpad_read_offloaded" in names
        assert "scratchpad_search_offloaded" in names
        assert len(names) == 14
