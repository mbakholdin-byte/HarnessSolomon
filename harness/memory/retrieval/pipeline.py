"""Retrieval pipeline orchestrator (Phase 1, Step 7).

The pipeline glues together a retriever, a reranker, and an
assembler. It owns the contract — callers see a single
``query(text)`` method that returns a ready-to-prompt string.

The components are pluggable (Protocol-typed), so swapping the
BM25 retriever for a Qdrant dense retriever, or the Identity
reranker for bge-reranker-v2-m3, is a one-line change at the
construction site.
"""
from __future__ import annotations

from harness.memory.retrieval.assembler import ContextAssembler
from harness.memory.retrieval.bm25 import Retriever
from harness.memory.retrieval.reranker import Reranker

#: Default number of candidates to pull from the retriever
#: before passing to the reranker. Per the Phase 1 design:
#: "hybrid (BM25 + vector) -> top-50".
DEFAULT_CANDIDATE_K: int = 50

#: Default number of reranked results to assemble into context.
#: Per the Phase 1 design: "cross-encoder rerank -> top-10".
DEFAULT_TOP_K: int = 10


class RetrievalPipeline:
    """End-to-end retrieval: query -> candidates -> rerank -> assemble.

    Args:
        retriever: Sparse (BM25) or dense (vector) retriever.
        reranker:  Cross-encoder reranker (IdentityReranker for tests).
        assembler: Output formatter (ContextAssembler by default).
    """

    def __init__(
        self,
        retriever: Retriever,
        reranker: Reranker,
        assembler: ContextAssembler | None = None,
    ) -> None:
        self.retriever = retriever
        self.reranker = reranker
        self.assembler = assembler or ContextAssembler()

    def query(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        candidate_k: int = DEFAULT_CANDIDATE_K,
    ) -> str:
        """Run a full retrieval → assembly cycle and return a string.

        Args:
            query:        Free-text query from the user / LLM.
            top_k:        Max entries to put into the assembled context.
            candidate_k:  Max entries to pull from the retriever
                          (before the reranker narrows them down).
                          Must be >= top_k.
        """
        if candidate_k < top_k:
            # The reranker can't produce more than the retriever gives it
            candidate_k = top_k
        candidates = self.retriever.retrieve(query, k=candidate_k)
        reranked = self.reranker.rerank(query, candidates, top_k=top_k)
        return self.assembler.assemble(query, reranked)


__all__ = [
    "RetrievalPipeline",
    "DEFAULT_CANDIDATE_K",
    "DEFAULT_TOP_K",
]
