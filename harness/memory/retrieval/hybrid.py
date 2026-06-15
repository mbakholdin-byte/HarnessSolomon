"""Phase 3: Hybrid retriever (BM25 + dense via RRF).

Reciprocal Rank Fusion (RRF) is the standard cheap hybrid that
beats either retriever alone:

    score(d) = sum over retrievers r: 1 / (rrf_k + rank_r(d))

where ``rank_r(d)`` is the 1-based rank of document ``d`` in
retriever ``r``'s output, and ``rrf_k`` is a smoothing constant
(60 is the original Cormack et al. value; lower k makes top
ranks weigh more, higher k smooths them out).

The hybrid fuses the two ranked lists without score normalisation
(BM25 scores and cosine scores live on different scales). This is
the key advantage of RRF over weighted-sum approaches.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from harness.memory.schema import Memory

logger = logging.getLogger(__name__)


@runtime_checkable
class _ScoredRetriever(Protocol):
    """Minimal interface for hybrid fusion.

    Both ``BM25Retriever.retrieve`` and ``DenseRetriever.retrieve``
    return ``list[tuple[Memory, float]]`` — this is the same shape,
    so a duck-typed callable works.
    """

    async def retrieve(
        self, query: str, k: int = 5,
    ) -> list[tuple[Memory, float]]: ...


class HybridRetriever:
    """Reciprocal Rank Fusion over two retrievers.

    Args:
        bm25:   The lexical retriever (or any retriever with
                ``retrieve(query, k) -> list[tuple[Memory, float]]``).
        dense:  The dense retriever. Typically ``DenseRetriever``
                but any conforming Protocol works.
        rrf_k:  RRF smoothing constant. Default 60 (Cormack 2009).
        fetch_k: Per-retriever top-k to fetch. Default 20. We
                 over-fetch because the final fused list is
                 ``k`` items and we want headroom.
    """

    def __init__(
        self,
        bm25: _ScoredRetriever,
        dense: _ScoredRetriever,
        *,
        rrf_k: int = 60,
        fetch_k: int = 20,
    ) -> None:
        self._bm25 = bm25
        self._dense = dense
        self._rrf_k = max(0, rrf_k)
        self._fetch_k = max(1, fetch_k)

    async def retrieve(
        self, query: str, k: int = 5,
    ) -> list[tuple[Memory, float]]:
        """Return top-k ``(Memory, rrf_score)`` pairs.

        RRF scores are not bounded; typical values are 0.01-0.05.
        """
        # Fetch from both retrievers concurrently.
        import asyncio
        bm25_hits, dense_hits = await asyncio.gather(
            self._bm25.retrieve(query, k=self._fetch_k),
            self._dense.retrieve(query, k=self._fetch_k),
        )
        scores: dict[str, float] = {}
        # Track which Memory corresponds to each id (we use the
        # Memory's content as a fallback id; if multiple Memories
        # have identical content we deduplicate via a list).
        id_to_mem: dict[str, Memory] = {}
        for rank, (mem, _score) in enumerate(bm25_hits, start=1):
            mid = id(mem)
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (self._rrf_k + rank)
            id_to_mem[mid] = mem
        for rank, (mem, _score) in enumerate(dense_hits, start=1):
            mid = id(mem)
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (self._rrf_k + rank)
            id_to_mem[mid] = mem
        # Sort by score descending.
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        return [(id_to_mem[mid], score) for mid, score in ranked[:k]]
