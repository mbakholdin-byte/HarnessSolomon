"""Rerankers (Phase 1, Step 7).

A reranker takes a list of (Memory, score) candidates and re-orders
them — typically with a more expensive (and more accurate) cross-
encoder model like bge-reranker-v2-m3.

For Phase 1 we ship an ``IdentityReranker`` that simply slices the
top-K. The interface is what matters; Phase 2 (or any plugin)
can drop in a real cross-encoder without touching the pipeline.
"""
from __future__ import annotations

from typing import Protocol

from harness.memory.schema import Memory


class Reranker(Protocol):
    """Protocol for a reranker. The pipeline doesn't care about
    the model — it just needs a method that takes candidates
    and a top_k and returns the top_k.
    """

    def rerank(
        self,
        query: str,
        candidates: list[tuple[Memory, float]],
        top_k: int,
    ) -> list[tuple[Memory, float]]:
        """Return at most ``top_k`` (Memory, score) tuples, score desc."""
        ...


class IdentityReranker:
    """No-op reranker that preserves input order (sliced to top_k).

    Useful for unit tests, when ``top_k <= len(candidates)`` from
    the retriever, and as a placeholder until a real cross-encoder
    is wired up. The score is passed through unchanged.
    """

    def rerank(
        self,
        query: str,
        candidates: list[tuple[Memory, float]],
        top_k: int,
    ) -> list[tuple[Memory, float]]:
        if top_k <= 0 or not candidates:
            return []
        return candidates[:top_k]


__all__ = ["IdentityReranker", "Reranker"]
