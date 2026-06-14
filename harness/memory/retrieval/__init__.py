"""Solomon Harness — retrieval pipeline (Phase 1, Step 7).

The pipeline is:

    query
      -> BM25 (sparse) + vector (dense)  -> top-50 candidates
      -> cross-encoder rerank              -> top-10
      -> context assembly                  -> LLM-ready string

We do NOT have a real vector store in Phase 1. The pipeline is
implemented as a thin orchestrator that delegates the heavy
lifting to pluggable ``Retriever`` and ``Reranker`` components.
The default in-memory implementations are:

  - ``BM25Retriever``     — pure-Python BM25 over a corpus
  - ``IdentityReranker``  — passes the candidates through (for
                            tests; Phase 2 swaps in bge-reranker-v2-m3)
  - ``ContextAssembler``  — concatenates the top-K memories,
                            truncating at a char budget

The pluggable design means Phase 2 (or anyone with a Qdrant
collection) can drop in a real vector retriever without
touching the pipeline.
"""
from harness.memory.retrieval.assembler import ContextAssembler, DEFAULT_MAX_CHARS
from harness.memory.retrieval.bm25 import BM25Retriever, Retriever
from harness.memory.retrieval.pipeline import (
    DEFAULT_CANDIDATE_K,
    DEFAULT_TOP_K,
    RetrievalPipeline,
)
from harness.memory.retrieval.reranker import IdentityReranker, Reranker

__all__ = [
    "BM25Retriever",
    "ContextAssembler",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_CANDIDATE_K",
    "DEFAULT_TOP_K",
    "IdentityReranker",
    "Reranker",
    "RetrievalPipeline",
    "Retriever",
]
