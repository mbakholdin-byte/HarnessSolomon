"""Phase 5.2B — Length-normalized re-ranker (stdlib-only).

Re-sorts BM25 retrieval results by a length-normalised score so
that very short or very long documents don't dominate the top-K
purely on token-frequency grounds.

Formula
-------

::

    score = bm25_score * (1 / log(length + e))

Where ``length`` is the document's character count and ``e`` is
Euler's number (the ``+ e`` avoids division by zero when
``length == 0``; ``log(0 + e) == 1`` so a zero-length doc keeps its
raw BM25 score, which is itself ~0 for BM25, so the net effect is
neutral).

Intuition: BM25 already has a length normalisation (the ``b``
field-length saturation parameter), but the eval corpus is small
enough that extreme outliers (a 1-char doc matching the query token,
or a 5000-char doc that mentions the token once) still skew
precision@5. Multiplying by ``1 / log(len + e)`` dampens both ends
without the complexity of a cross-encoder model.

This is a **post-retrieval** re-ranker: it takes the BM25 top-N
(typically N=20 or N=50) and re-sorts, then the caller takes the
top-K. It does NOT replace BM25 retrieval — it refines the ranking
of already-retrieved docs.

Trust boundary
--------------

Imports only :mod:`harness.memory.schema` and stdlib (``math``).
No :mod:`harness.agents`, :mod:`harness.server`, or ML deps.
Auto-checked by ``tests/eval/test_eval_trust_boundary.py``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from harness.memory.schema import Memory


# === Config ============================================================


@dataclass(frozen=True)
class RerankerConfig:
    """Configuration for :class:`LengthNormalizedReranker`.

    Attributes:
        e_offset: Constant added to length before taking the log.
            Default ``math.e`` so ``log(0 + e) == 1`` (zero-length
            docs keep their raw score). Set to a larger value for
            gentler normalisation.
        min_length: Documents shorter than this are clamped to it
            before normalisation, so a 1-char doc isn't
            disproportionately penalised. Default 1.
    """

    e_offset: float = math.e
    min_length: int = 1


# === Re-ranker =========================================================


class LengthNormalizedReranker:
    """Post-retrieval length-normalised re-ranker.

    Usage::

        reranker = LengthNormalizedReranker()
        reranked = reranker.rerank(query, retrieved_docs)

    The ``query`` is accepted for API symmetry (future cross-encoder
    re-rankers will use it) but is NOT used in the current length-
    normalisation formula. The sort is stable on the original BM25
    order, so docs with identical re-ranked scores keep their
    retrieval order.
    """

    def __init__(self, config: RerankerConfig | None = None) -> None:
        self.config: RerankerConfig = (
            config if config is not None else RerankerConfig()
        )

    def score(self, doc: Memory, bm25_score: float) -> float:
        """Compute the length-normalised score for one doc.

        Args:
            doc: The retrieved document.
            bm25_score: The BM25 score the retriever assigned to
                this doc for the current query.

        Returns:
            ``bm25_score / log(max(len(content), min_length) + e_offset)``.
        """
        cfg = self.config
        length = max(len(doc.content), cfg.min_length)
        denom = math.log(length + cfg.e_offset)
        if denom <= 0:
            # Defensive: log can be ≤ 0 for very small inputs with
            # a small e_offset. Fall back to raw BM25 to avoid
            # division-by-zero or sign flip.
            return bm25_score
        return bm25_score / denom

    def rerank(
        self,
        query: str,
        docs: list[tuple[Memory, float]],
    ) -> list[tuple[Memory, float]]:
        """Re-sort ``docs`` by the length-normalised score.

        Args:
            query: The user query (unused by the current formula,
                accepted for API symmetry with future cross-encoder
                re-rankers).
            docs: List of ``(Memory, bm25_score)`` tuples from the
                retriever. Order is the retriever's ranking.

        Returns:
            New list sorted by re-ranked score descending. Stable
            on the input order for ties (Python's ``sorted`` is
            stable). Each tuple's score is updated to the
            normalised value so callers can inspect the new ranking.
        """
        scored = [
            (doc, self.score(doc, bm25)) for doc, bm25 in docs
        ]
        # Sort by normalised score descending; stable on ties so
        # the original BM25 order is preserved within a tie group.
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored


__all__ = [
    "LengthNormalizedReranker",
    "RerankerConfig",
]
