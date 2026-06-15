"""Phase 3: tests for UnifiedMemory embedder integration.

The ``UnifiedMemory.write`` extension accepts an ``embedder=`` param
in its constructor. When set, every write computes the embedding
and stores it in ``metadata.embedding`` + ``metadata.embedding_version``.
The new ``search_scored`` method uses the embedder for dense retrieval;
``search`` is unchanged (backward compat).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from harness.memory.schema import Memory


@pytest.fixture
def mem_setup(tmp_path: Any) -> tuple[Any, Any]:
    """Build a UnifiedMemory + fake embedder, isolated dir."""
    from harness.memory.unified import UnifiedMemory

    hmem_dir = tmp_path / "hmem"
    mem0_dir = tmp_path / "mem0"
    hybrid_dir = tmp_path / "hybrid"
    file_dir = tmp_path / "files"
    for d in (hmem_dir, mem0_dir, hybrid_dir, file_dir):
        d.mkdir(parents=True, exist_ok=True)

    embedder = MagicMock()
    embedder.model_id = "test-model@1"

    async def fake_embed(texts: list[str]) -> np.ndarray:
        # Deterministic random vectors based on content hash.
        out = np.zeros((len(texts), 4), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, hash(t) % 4] = 1.0
        return out

    async def fake_query(text: str) -> np.ndarray:
        out = np.zeros(4, dtype=np.float32)
        out[hash(text) % 4] = 1.0
        return out

    embedder.embed_documents = fake_embed
    embedder.embed_query = fake_query
    mem = UnifiedMemory(
        hmem_dir=hmem_dir,
        mem0_dir=mem0_dir,
        hybrid_dir=hybrid_dir,
        file_dir=file_dir,
        agent_id="phase3",
        embedder=embedder,
    )
    return mem, embedder


class TestUnifiedMemoryEmbedder:
    def test_constructor_accepts_embedder(
        self, mem_setup: tuple[Any, Any],
    ) -> None:
        mem, embedder = mem_setup
        assert mem.embedder is embedder

    def test_constructor_default_embedder_is_none(
        self, tmp_path: Any,
    ) -> None:
        from harness.memory.unified import UnifiedMemory

        for d in ("hmem", "mem0", "hybrid", "files"):
            (tmp_path / d).mkdir(parents=True, exist_ok=True)
        mem = UnifiedMemory(
            hmem_dir=tmp_path / "hmem",
            mem0_dir=tmp_path / "mem0",
            hybrid_dir=tmp_path / "hybrid",
            file_dir=tmp_path / "files",
            agent_id="phase3",
        )
        assert mem.embedder is None

    def test_write_stores_embedding_in_metadata(
        self, mem_setup: tuple[Any, Any],
    ) -> None:
        mem, _ = mem_setup
        m = Memory(
            id="m1",
            layer="L2",
            source="manual",
            content="hello world",
            tags=[],
            metadata={"agent_id": "phase3"},
        )
        mem.write(m)
        # The memory was stamped with the embedding.
        assert "embedding" in m.metadata
        assert "embedding_version" in m.metadata
        assert m.metadata["embedding_version"] == "test-model@1"
        # The embedding is a list of floats (4-dim, matching fake).
        assert isinstance(m.metadata["embedding"], list)
        assert len(m.metadata["embedding"]) == 4


class TestUnifiedMemorySearchScored:
    def test_search_scored_no_embedder_falls_back(
        self, tmp_path: Any,
    ) -> None:
        from harness.memory.unified import UnifiedMemory

        for d in ("hmem", "mem0", "hybrid", "files"):
            (tmp_path / d).mkdir(parents=True, exist_ok=True)
        mem = UnifiedMemory(
            hmem_dir=tmp_path / "hmem",
            mem0_dir=tmp_path / "mem0",
            hybrid_dir=tmp_path / "hybrid",
            file_dir=tmp_path / "files",
            agent_id="phase3",
            embedder=None,  # explicit: no embedder
        )
        # No write — search returns empty.
        result = mem.search_scored("anything", k=5)
        assert result == []

    def test_search_returns_unchanged_shape(
        self, tmp_path: Any,
    ) -> None:
        # The old ``search`` method must still return ``list[Memory]``
        # (not a list of tuples). This is the backward-compat
        # contract that Phase 2.5 callers depend on.
        from harness.memory.unified import UnifiedMemory

        for d in ("hmem", "mem0", "hybrid", "files"):
            (tmp_path / d).mkdir(parents=True, exist_ok=True)
        mem = UnifiedMemory(
            hmem_dir=tmp_path / "hmem",
            mem0_dir=tmp_path / "mem0",
            hybrid_dir=tmp_path / "hybrid",
            file_dir=tmp_path / "files",
            agent_id="phase3",
        )
        result = mem.search("hello")
        # The type is list[Memory], not list[tuple[Memory, float]].
        # Verify by checking that the first element (if any) is a
        # Memory instance — not a tuple.
        for item in result:
            assert isinstance(item, Memory)
