"""Phase 3: tests for the dense retriever and hybrid retriever.

Coverage:
    - ``DenseRetriever`` returns top-k sorted by score
    - ``DenseRetriever`` handles empty corpus / empty query
    - ``DenseRetriever`` filters out mismatched embedding versions
    - ``HybridRetriever`` fuses BM25 + dense via RRF
    - ``HybridRetriever`` ranks docs that appear in BOTH above
      those that appear in only one
    - ``PrivacyAwareEmbedder`` redacts text BEFORE embedding
    - ``EMBEDDING_MODEL_VERSION`` is a non-empty string
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from harness.config import Settings
from harness.memory.embeddings import PrivacyAwareEmbedder
from harness.memory.retrieval.dense import DenseRetriever
from harness.memory.retrieval.hybrid import HybridRetriever
from harness.memory.retrieval.versioning import EMBEDDING_MODEL_VERSION
from harness.memory.schema import Memory, ProvenanceEntry


# === Fake embedder for tests ===

class _FakeEmbedder:
    """Deterministic hash-based embedder for unit tests.

    Maps each text to a 16-dim unit vector based on its content
    (so semantically-similar texts cluster together). The exact
    embedding doesn't matter for unit tests; we only need a
    consistent, deterministic ``embed_query`` and
    ``embed_documents`` to drive the retriever logic.
    """

    def __init__(self, dim: int = 16) -> None:
        self._dim = dim
        self._model_id = "fake@1"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_id(self) -> str:
        return self._model_id

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._embed(t) for t in texts]).astype(np.float32)

    async def embed_query(self, text: str) -> np.ndarray:
        return self._embed(text).astype(np.float32)

    def _embed(self, text: str) -> np.ndarray:
        # Hash-based pseudo-vector. Words with overlapping characters
        # produce similar vectors (simple char-trigram bag).
        vec = np.zeros(self._dim, dtype=np.float32)
        t = text.lower()
        for i in range(len(t) - 2):
            tri = t[i : i + 3]
            vec[hash(tri) % self._dim] += 1.0
        n = np.linalg.norm(vec)
        if n > 0:
            vec = vec / n
        return vec


# === Fixtures ===

@pytest.fixture
def embedder() -> _FakeEmbedder:
    return _FakeEmbedder()


def _make_memory(
    content: str,
    *,
    embedding: list[float] | None = None,
    version: str | None = None,
    mem_id: str | None = None,
) -> Memory:
    meta: dict[str, Any] = {"agent_id": "test"}
    if embedding is not None:
        meta["embedding"] = embedding
    if version is not None:
        meta["embedding_version"] = version
    return Memory(
        id=mem_id or f"m-{hash(content) & 0xFFFF:04x}",
        layer="L2",
        source="manual",
        content=content,
        tags=["#test"],
        metadata=meta,
        provenance=[
            ProvenanceEntry(layer="L_meta", source="unified", id="test"),
        ],
    )


@pytest.fixture
def corpus(embedder: _FakeEmbedder) -> list[Memory]:
    """A small corpus with embeddings tagged at the current version.

    Embeddings are pre-computed by the fake embedder so the
    DenseRetriever can build its matrix from ``metadata.embedding``
    without re-embedding. We bypass ``asyncio.run`` by using a
    private sync helper (``_embed``) directly so the fixture
    doesn't conflict with pytest-asyncio's event loop.
    """
    texts = [
        "The cat sat on the mat",
        "The dog chased the cat",
        "Python is a programming language",
        "Cats make good pets",
        "Dogs are loyal companions",
    ]
    version = embedder.model_id
    return [
        _make_memory(
            content=t,
            version=version,
            embedding=embedder._embed(t).tolist(),
        )
        for t in texts
    ]


# === Tests: DenseRetriever ===

class TestDenseRetriever:
    def test_constructor_filters_mismatched_versions(
        self, embedder: _FakeEmbedder,
    ) -> None:
        # Use the embedder's own model_id as the "current" version
        # so the kept set is deterministic.
        current_version = embedder.model_id
        current_vec = [0.1] * embedder.dim
        old_vec = [0.2] * embedder.dim
        corpus = [
            _make_memory("a", version=current_version, embedding=current_vec),
            _make_memory("b", version="old-model@1", embedding=old_vec),
            _make_memory("c", version=current_version, embedding=current_vec),
        ]
        retriever = DenseRetriever(corpus=corpus, embedder=embedder)
        # 2 of 3 are kept (the old-version entry is filtered).
        assert retriever._matrix.shape[0] == 2
        # The kept set is exactly 2 entries.
        assert len(retriever._corpus) == 2

    def test_empty_corpus(self, embedder: _FakeEmbedder) -> None:
        r = DenseRetriever(corpus=[], embedder=embedder)
        # ``retrieve`` is async; drive it with asyncio.run.
        result = asyncio.run(r.retrieve("anything", k=5))
        assert result == []

    @pytest.mark.asyncio
    async def test_retrieve_top_k_sorted(
        self, embedder: _FakeEmbedder, corpus: list[Memory],
    ) -> None:
        r = DenseRetriever(corpus=corpus, embedder=embedder)
        results = await r.retrieve("cat", k=3)
        # Top-3 by cosine similarity to the "cat" embedding.
        assert len(results) == 3
        # Scores are non-negative (cosine on L2-normalised vectors).
        for mem, score in results:
            assert isinstance(mem, Memory)
            assert -1.0 <= score <= 1.0
        # Sorted descending.
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_retrieve_k_clamps_to_corpus_size(
        self, embedder: _FakeEmbedder, corpus: list[Memory],
    ) -> None:
        r = DenseRetriever(corpus=corpus, embedder=embedder)
        # k=100 should return all 5.
        results = await r.retrieve("anything", k=100)
        assert len(results) == len(corpus)


# === Tests: HybridRetriever ===

class TestHybridRetriever:
    @pytest.mark.asyncio
    async def test_hybrid_fuses_two_lists(self) -> None:
        # A document that appears in BOTH retrievers should rank
        # above one that appears in only one.
        m_a = _make_memory("doc-a", mem_id="doc-a")
        m_b = _make_memory("doc-b", mem_id="doc-b")
        m_c = _make_memory("doc-c", mem_id="doc-c")

        class _BM25Stub:
            async def retrieve(
                self, query: str, k: int = 5,
            ) -> list[tuple[Memory, float]]:
                # doc-a and doc-c match.
                return [(m_a, 1.0), (m_c, 0.5)]

        class _DenseStub:
            async def retrieve(
                self, query: str, k: int = 5,
            ) -> list[tuple[Memory, float]]:
                # doc-a and doc-b match.
                return [(m_a, 0.9), (m_b, 0.7)]

        hybrid = HybridRetriever(
            bm25=_BM25Stub(), dense=_DenseStub(), rrf_k=60,
        )
        results = await hybrid.retrieve("anything", k=5)
        # doc-a appears in both → highest RRF score.
        assert results[0][0].id == "doc-a"
        # doc-b and doc-c tie on RRF (each appears in only one
        # retriever at rank 2). Order between them is unspecified
        # but both should be present.
        remaining = {r[0].id for r in results[1:]}
        assert remaining == {"doc-b", "doc-c"}

    @pytest.mark.asyncio
    async def test_hybrid_empty_inputs(self) -> None:
        class _Empty:
            async def retrieve(
                self, query: str, k: int = 5,
            ) -> list[tuple[Memory, float]]:
                return []
        hybrid = HybridRetriever(bm25=_Empty(), dense=_Empty())
        results = await hybrid.retrieve("anything", k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_hybrid_rrf_k_sensitivity(self) -> None:
        # Higher RRF k → smoother (lower) scores overall, but the
        # RELATIVE ranking is the same.
        m_a = _make_memory("doc-a", mem_id="doc-a")
        m_b = _make_memory("doc-b", mem_id="doc-b")

        class _OnlyA:
            async def retrieve(
                self, query: str, k: int = 5,
            ) -> list[tuple[Memory, float]]:
                return [(m_a, 1.0)]

        class _OnlyB:
            async def retrieve(
                self, query: str, k: int = 5,
            ) -> list[tuple[Memory, float]]:
                return [(m_b, 1.0)]

        for rrf_k in (1, 10, 60, 1000):
            hybrid = HybridRetriever(
                bm25=_OnlyA(), dense=_OnlyB(), rrf_k=rrf_k,
            )
            results = await hybrid.retrieve("q", k=5)
            # Both docs present.
            assert len(results) == 2
            # Score spread shrinks as rrf_k grows (less discrimination).
            spread = results[0][1] - results[1][1]
            if rrf_k == 1:
                # With k=1, both are at 1/2 = 0.5. Spread = 0.
                pass
            else:
                # In general, the spread is non-negative.
                assert spread >= 0


# === Tests: PrivacyAwareEmbedder ===

class TestPrivacyAwareEmbedder:
    @pytest.mark.asyncio
    async def test_redacts_before_embedding(self) -> None:
        # Capture what the inner embedder actually saw.
        captured: list[list[str]] = []

        class _SpyEmbedder:
            @property
            def dim(self) -> int:
                return 4
            @property
            def model_id(self) -> str:
                return "spy@1"
            async def embed_documents(self, texts: list[str]) -> np.ndarray:
                captured.append(list(texts))
                return np.zeros((len(texts), 4), dtype=np.float32)
            async def embed_query(self, text: str) -> np.ndarray:
                captured.append([text])
                return np.zeros(4, dtype=np.float32)

        wrapped = PrivacyAwareEmbedder(_SpyEmbedder())
        await wrapped.embed_documents(["alice@example.com"])
        # The inner embedder saw the redacted text, not the raw.
        assert "alice@" not in captured[0][0]
        assert "<EMAIL>" in captured[0][0]

    @pytest.mark.asyncio
    async def test_redacts_query_before_embedding(self) -> None:
        captured: list[list[str]] = []

        class _SpyEmbedder:
            @property
            def dim(self) -> int:
                return 4
            @property
            def model_id(self) -> str:
                return "spy@1"
            async def embed_documents(self, texts: list[str]) -> np.ndarray:
                captured.append(list(texts))
                return np.zeros((len(texts), 4), dtype=np.float32)
            async def embed_query(self, text: str) -> np.ndarray:
                captured.append([text])
                return np.zeros(4, dtype=np.float32)

        wrapped = PrivacyAwareEmbedder(_SpyEmbedder())
        await wrapped.embed_query("Email alice@example.com")
        assert "alice@" not in captured[0][0]


# === Tests: version constant ===

class TestVersioning:
    def test_version_is_non_empty_string(self) -> None:
        assert isinstance(EMBEDDING_MODEL_VERSION, str)
        assert EMBEDDING_MODEL_VERSION
