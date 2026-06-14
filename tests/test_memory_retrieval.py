"""Tests for the retrieval pipeline (Phase 1, Step 7).

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
  - ``TruncatingAssembler`` — concatenates the top-K memories,
                              truncating at a token budget

The pluggable design means Phase 2 (or anyone with a Qdrant
collection) can drop in a real vector retriever without
touching the pipeline.
"""
from __future__ import annotations

from typing import Any

import pytest

from harness.memory.retrieval import (
    BM25Retriever,
    ContextAssembler,
    IdentityReranker,
    RetrievalPipeline,
)
from harness.memory.schema import Memory


# === Test corpora ===

@pytest.fixture
def corpus() -> list[Memory]:
    """A small test corpus with clear topical distinctions."""
    return [
        Memory(id="t-1", content="MiniMax M2.7 is the primary T3 model", layer="L2", source="mem0"),
        Memory(id="t-2", content="GLM-4.7 is the cheap T3 model", layer="L2", source="mem0"),
        Memory(id="t-3", content="Moonshot v1 128k is the long-context T3", layer="L2", source="mem0"),
        Memory(id="t-4", content="Phase 0 Web MVP shipped 2026-06-14", layer="L3", source="hybrid"),
        Memory(id="t-5", content="Lesson: 4 parallel subagent runs work for independent steps", layer="L1", source="hmem"),
        Memory(id="t-6", content="Moonshot streaming uses sync drain to avoid generator __aiter__ bug", layer="L3", source="hybrid"),
        Memory(id="t-7", content="Decision: ports 8000-9000 reserved by hns on Windows 11", layer="L1", source="hmem"),
    ]


# === BM25Retriever ===

def test_bm25_finds_exact_term(corpus: list[Memory]) -> None:
    r = BM25Retriever(corpus)
    results = r.retrieve("moonshot", k=3)
    ids = [m.id for m, _ in results]
    # Both moonshot entries should appear
    assert "t-3" in ids
    assert "t-6" in ids


def test_bm25_ranks_better_match_higher(corpus: list[Memory]) -> None:
    """When two documents match, the one with the term more densely wins."""
    r = BM25Retriever(corpus)
    results = r.retrieve("moonshot", k=2)
    # t-3 says "Moonshot v1 128k is the long-context T3" — short and on-topic
    # t-6 says "Moonshot streaming uses sync drain to avoid generator __aiter__ bug"
    # The shorter, on-topic one ranks higher
    assert results[0][0].id == "t-3"


def test_bm25_returns_empty_for_no_match(corpus: list[Memory]) -> None:
    r = BM25Retriever(corpus)
    results = r.retrieve("xyzzy", k=3)
    assert results == []


def test_bm25_k_limits_results(corpus: list[Memory]) -> None:
    r = BM25Retriever(corpus)
    results = r.retrieve("model", k=2)
    assert len(results) <= 2


def test_bm25_zero_k_returns_empty(corpus: list[Memory]) -> None:
    r = BM25Retriever(corpus)
    assert r.retrieve("moonshot", k=0) == []


# === IdentityReranker ===

def test_identity_reranker_passes_top_k_through(corpus: list[Memory]) -> None:
    """IdentityReranker: top_k just slices the input."""
    r = IdentityReranker()
    candidates = [(m, 1.0) for m in corpus]
    out = r.rerank("any query", candidates, top_k=3)
    assert [m.id for m, _ in out] == ["t-1", "t-2", "t-3"]


def test_identity_reranker_preserves_score(corpus: list[Memory]) -> None:
    r = IdentityReranker()
    candidates = [(corpus[0], 0.5), (corpus[1], 0.9)]
    out = r.rerank("q", candidates, top_k=2)
    assert out[0][1] == 0.5
    assert out[1][1] == 0.9


# === ContextAssembler ===

def test_assembler_concatenates_with_headers() -> None:
    """Assembler emits one [id] header per Memory + content."""
    a = ContextAssembler()
    items = [
        (Memory(id="a", content="alpha", layer="L2", source="mem0"), 0.9),
        (Memory(id="b", content="beta", layer="L2", source="mem0"), 0.7),
    ]
    out = a.assemble("the query", items)
    assert "[a]" in out
    assert "alpha" in out
    assert "[b]" in out
    assert "beta" in out


def test_assembler_orders_by_input(corpus: list[Memory]) -> None:
    """Output order matches input order (reranker decides this upstream)."""
    a = ContextAssembler()
    items = [(corpus[2], 0.5), (corpus[0], 0.9)]
    out = a.assemble("q", items)
    # t-3 header must come before t-1
    assert out.index("[t-3]") < out.index("[t-1]")


def test_assembler_respects_max_chars(corpus: list[Memory]) -> None:
    """When over budget, the output is truncated and a marker is appended."""
    a = ContextAssembler(max_chars=80)
    items = [(m, 1.0) for m in corpus]
    out = a.assemble("q", items)
    assert len(out) <= 80 + 30  # some slack for the truncation marker
    assert "truncated" in out.lower() or "…" in out


# === RetrievalPipeline ===

def test_pipeline_end_to_end(corpus: list[Memory]) -> None:
    """Full pipeline: BM25 -> Identity rerank -> assemble."""
    p = RetrievalPipeline(
        retriever=BM25Retriever(corpus),
        reranker=IdentityReranker(),
        assembler=ContextAssembler(),
    )
    out = p.query("moonshot", top_k=2, candidate_k=10)
    assert isinstance(out, str)
    # At least one moonshot doc
    assert "moonshot" in out.lower()


def test_pipeline_returns_string_for_empty_corpus() -> None:
    p = RetrievalPipeline(
        retriever=BM25Retriever([]),
        reranker=IdentityReranker(),
        assembler=ContextAssembler(),
    )
    out = p.query("anything", top_k=5, candidate_k=10)
    # No matches → empty context
    assert out == ""


def test_pipeline_top_k_limits_assembly(corpus: list[Memory]) -> None:
    """top_k=1 means at most 1 Memory in the output."""
    p = RetrievalPipeline(
        retriever=BM25Retriever(corpus),
        reranker=IdentityReranker(),
        assembler=ContextAssembler(),
    )
    out = p.query("model", top_k=1, candidate_k=10)
    # Only 1 header line expected
    assert out.count("\n[") <= 1  # 0 or 1 newline-prefixed headers


def test_pipeline_candidate_k_limits_retriever(corpus: list[Memory]) -> None:
    """candidate_k caps how many results the retriever returns."""
    p = RetrievalPipeline(
        retriever=BM25Retriever(corpus),
        reranker=IdentityReranker(),
        assembler=ContextAssembler(),
    )
    # candidate_k=2 means the retriever gets at most 2 candidates
    # before the reranker sees them
    out = p.query("model", top_k=5, candidate_k=2)
    # With only 2 candidates, even a top_k=5 rerank can't produce more
    assert out.count("\n[") <= 2


def test_pipeline_default_top_k_is_10() -> None:
    """Pipeline default top_k is 10 per the Phase 1 design."""
    import inspect
    sig = inspect.signature(RetrievalPipeline.query)
    assert sig.parameters["top_k"].default == 10


def test_pipeline_default_candidate_k_is_50() -> None:
    """Pipeline default candidate_k is 50 per the Phase 1 design."""
    import inspect
    sig = inspect.signature(RetrievalPipeline.query)
    assert sig.parameters["candidate_k"].default == 50
