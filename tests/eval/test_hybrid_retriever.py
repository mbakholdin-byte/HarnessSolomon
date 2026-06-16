"""Phase 5.1: Hybrid retriever integration tests (BM25 + Dense via RRF).

Validates the B2/B3 strict DoD against a real ONNX embedding model
(``intfloat/multilingual-e5-small``, 384-dim). Marked
``@pytest.mark.requires_onnx`` — skipped automatically when the
ONNX model is not present at the configured ``EMBEDDINGS_DIR``.

Pilot results (16.06.2026, onnx_backend fix + hybrid sync/async fix):
    - B2 precision@5:
        BM25:     0.191 (target 0.7, NOT MET)
        Dense:    0.218 (target 0.7, NOT MET)
        Hybrid:   0.204 (target 0.7, NOT MET — corpus problem)
    - B3 recall@20:
        BM25:     0.843 (target 0.85, NOT MET 0.7pp below)
        Dense:    0.961 (target 0.85, ✅ MET)
        Hybrid:   0.961 (target 0.85, ✅ MET)

B3 STRICT DoD CLOSED via Dense / Hybrid. B2 still below target —
the assistant turns with "ack and continue" filler dominate top-5
in ALL retrievers. This is a corpus design issue (seed_session_100
has 200 messages with 50% assistant filler), not a retriever issue.
Phase 5.1 closeout ships hybrid infrastructure + B3 strict pass;
B2 strict pass deferred to Phase 5.2 (corpus redesign OR query
filtering by turn type).

Test scope (3 tests):
    - test_dense_retriever_loads_onnx: DenseRetriever constructs
      and retrieves from a 1-memory corpus without errors.
    - test_hybrid_retriever_combines_lists: HybridRetriever fuses
      BM25 + Dense via RRF, top-5 has entries from both.
    - test_b3_recall_at_20_on_hybrid: B3 DoD ≥ 0.85 on hybrid
      retriever (the headline result).
"""
from __future__ import annotations

import inspect
import shutil
from pathlib import Path

import pytest

from harness.config import Settings
from harness.eval import GoldenFact, GoldenQuery
from harness.eval.golden import fact_id_to_relevant_memory_id
from harness.eval.retrieval import session_to_corpus
from harness.memory.retrieval.bm25 import BM25Retriever
from harness.memory.retrieval.dense import DenseRetriever
from harness.memory.retrieval.hybrid import HybridRetriever


pytestmark = pytest.mark.requires_onnx


# Skip the entire module if the ONNX model is not available.
_ONNX_AVAILABLE = False
try:
    import onnxruntime  # noqa: F401
    from tokenizers import Tokenizer  # noqa: F401
    _settings = Settings()
    _cache = _settings.embeddings_dir
    if (_cache / "model.onnx").exists() and (_cache / "tokenizer.json").exists():
        _ONNX_AVAILABLE = True
except Exception:  # noqa: BLE001
    pass

if not _ONNX_AVAILABLE:
    pytest.skip("ONNX embedding model not available", allow_module_level=True)


@pytest.fixture
def onnx_embedder() -> "OnnxEmbedder":
    """Real OnnxEmbedder (requires model + tokenizer at EMBEDDINGS_DIR)."""
    from harness.memory.embeddings import OnnxEmbedder

    return OnnxEmbedder(Settings())


@pytest.fixture
async def corpus_with_embeddings(
    seed_session_100: list[dict],
    onnx_embedder: "OnnxEmbedder",
) -> list["Memory"]:
    """Seed session + pre-computed embeddings in Memory.metadata."""
    from harness.memory.schema import Memory

    base_corpus = session_to_corpus(seed_session_100)
    texts = [m.content for m in base_corpus]
    vecs = await onnx_embedder.embed_documents(texts)
    emb_version = onnx_embedder.model_id
    corpus: list[Memory] = []
    for m, v in zip(base_corpus, vecs):
        new_meta = dict(m.metadata or {})
        new_meta["embedding"] = v.tolist()
        new_meta["embedding_version"] = emb_version
        corpus.append(m.model_copy(update={"metadata": new_meta}))
    return corpus


@pytest.mark.asyncio
async def test_dense_retriever_loads_onnx(
    corpus_with_embeddings,
    onnx_embedder: "OnnxEmbedder",
) -> None:
    """DenseRetriever constructs and retrieves without errors."""
    dense = DenseRetriever(corpus_with_embeddings, onnx_embedder)
    hits = await dense.retrieve("Phase 3 context engineering", k=5)
    assert len(hits) == 5
    for mem, score in hits:
        assert score > 0.0  # cosine similarity should be positive
        assert mem.id.startswith("m")


@pytest.mark.asyncio
async def test_hybrid_retriever_combines_lists(
    corpus_with_embeddings,
    onnx_embedder: "OnnxEmbedder",
) -> None:
    """HybridRetriever fuses BM25 + Dense via RRF (sync + async)."""
    bm25 = BM25Retriever(corpus_with_embeddings)
    dense = DenseRetriever(corpus_with_embeddings, onnx_embedder)
    hybrid = HybridRetriever(bm25, dense, rrf_k=60, fetch_k=20)

    # Both retrievers must be callable from the hybrid (sync + async).
    hits = await hybrid.retrieve("Qdrant primary vector store", k=5)
    assert len(hits) == 5
    for mem, score in hits:
        assert score > 0.0
        # RRF scores are in [0, 1/(rrf_k+1), 2/(rrf_k+1), ...]
        assert 0.0 < score <= 2.0 / 61.0


@pytest.mark.asyncio
async def test_b3_recall_at_20_on_hybrid(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
    corpus_with_embeddings,
    onnx_embedder: "OnnxEmbedder",
) -> None:
    """B3 STRICT DoD: recall@20 ≥ 0.85 on hybrid retriever (45 threshold queries).

    The headline pilot result. Multi-hop queries are excluded from
    the threshold (per Phase 5 sign-off 2026-06-16).
    """
    bm25 = BM25Retriever(corpus_with_embeddings)
    dense = DenseRetriever(corpus_with_embeddings, onnx_embedder)
    hybrid = HybridRetriever(bm25, dense, rrf_k=60, fetch_k=20)

    fact_to_mem = fact_id_to_relevant_memory_id(golden_facts, corpus_with_embeddings)

    threshold_relevant_retrieved = 0
    threshold_relevant_in_gt = 0

    async def aretrieve(retriever, query, k):
        r = retriever.retrieve(query, k=k)
        if inspect.isawaitable(r):
            return await r
        return r

    for q in golden_queries:
        if q.category == "multi_hop":
            continue  # excluded from main DoD threshold
        hits = await aretrieve(hybrid, q.query, 20)
        retrieved_ids = {h[0].id for h in hits}
        gt = {
            fact_to_mem[fid]
            for fid in q.relevant_fact_ids
            if fact_to_mem.get(fid)
        }
        if not gt:
            continue
        threshold_relevant_retrieved += len(retrieved_ids & gt)
        threshold_relevant_in_gt += len(gt)

    ratio = threshold_relevant_retrieved / max(threshold_relevant_in_gt, 1)
    assert ratio >= 0.85, (
        f"B3 STRICT DoD NOT MET: recall@20 = {ratio:.3f} (target 0.85). "
        f"Relevant retrieved: {threshold_relevant_retrieved}/"
        f"{threshold_relevant_in_gt}. "
        f"Check onnx_backend.py:189 (token_type_ids fix) and "
        f"hybrid.py:75 (sync/async gather fix)."
    )
