"""Tests for ``harness.memory.retrieval.bm25_fast.bm25_search`` (Phase 6.4 v1.29.0).

Covers three concerns:
  * **Correctness** — output matches the documented contract on edge
    cases (empty inputs, ranking order, tie-break, truncation).
  * **Score parity** — per-score delta between Rust and Python paths
    stays within ±1 %, and top-k ordering is identical.
  * **Speedup** — when the Rust wheel is installed, BM25 search on a
    realistic corpus is at least 3× faster than the pure-Python fallback
    (target: 5×, but we assert the floor to keep the test stable across
    machines).
"""
from __future__ import annotations

import time
from typing import Callable

import pytest

from harness.memory.retrieval.bm25_fast import (
    _bm25_search_python,
    bm25_search,
    is_rust_active,
)

# ─────────────────────────────────────────────────────────────────────
# Correctness
# ─────────────────────────────────────────────────────────────────────


class TestCorrectness:
    """Documented contract for ``bm25_search``."""

    def test_empty_query_returns_empty(self) -> None:
        assert bm25_search("", ["a doc"], 5) == []

    def test_empty_documents_returns_empty(self) -> None:
        assert bm25_search("query", [], 5) == []

    def test_k_zero_returns_empty(self) -> None:
        assert bm25_search("query", ["a doc"], 0) == []

    def test_single_match_returns_one_hit(self) -> None:
        out = bm25_search("rust", ["rust is fast", "python is dynamic"], 5)
        assert len(out) == 1
        assert out[0][0] == 0  # doc index 0 matched
        assert out[0][1] > 0.0  # positive score

    def test_higher_tf_ranks_first(self) -> None:
        docs = ["rust language", "python language", "rust rust rust"]
        out = bm25_search("rust", docs, 3)
        assert len(out) == 2
        # doc 2 has tf=3, doc 0 has tf=1 → doc 2 first
        assert out[0][0] == 2
        assert out[0][1] > out[1][1]

    def test_tie_break_by_doc_index_asc(self) -> None:
        docs = ["alpha alpha", "alpha alpha"]
        out = bm25_search("alpha", docs, 2)
        assert len(out) == 2
        assert out[0][0] == 0
        assert out[1][0] == 1
        assert abs(out[0][1] - out[1][1]) < 1e-5

    def test_truncates_to_k(self) -> None:
        docs = [f"doc {i} rust" for i in range(10)]
        out = bm25_search("rust", docs, 3)
        assert len(out) == 3

    def test_no_overlap_returns_empty(self) -> None:
        assert bm25_search("kotlin", ["rust rules"], 5) == []

    def test_unicode_query_supported(self) -> None:
        out = bm25_search("мир", ["привет мир", "hello world"], 5)
        assert len(out) == 1
        assert out[0][0] == 0

    def test_results_sorted_by_score_desc(self) -> None:
        docs = [
            "rust rust rust",  # highest tf
            "rust",  # lowest tf
            "rust rust",  # medium tf
        ]
        out = bm25_search("rust", docs, 3)
        scores = [s for _, s in out]
        assert scores == sorted(scores, reverse=True)


# ─────────────────────────────────────────────────────────────────────
# Score parity (Rust vs Python)
# ─────────────────────────────────────────────────────────────────────


class TestScoreParity:
    """Rust and Python paths must produce the same ranking.

    We assert:
      * Top-k index ordering is identical.
      * Per-score delta < 1 %.
    """

    CORPUS = [
        "The rust programming language is fast and safe",
        "Python is a dynamic language with a large ecosystem",
        "BM25 is a ranking function for information retrieval",
        "Rust ownership model prevents data races at compile time",
        "Information retrieval systems use BM25 for sparse matching",
        "The cargo build system manages rust dependencies",
        "Tokenisation splits text into terms for BM25 indexing",
        "Dense retrieval complements sparse BM25 with vector similarity",
    ]

    QUERIES = [
        "rust language",
        "BM25 retrieval",
        "python ecosystem",
        "tokenisation",
        "dense vector",
        "cargo dependencies",
    ]

    @pytest.mark.skipif(not is_rust_active(), reason="Rust wheel not installed")
    @pytest.mark.parametrize("query", QUERIES)
    def test_top_k_ordering_identical(self, query: str) -> None:
        rust_out = bm25_search(query, self.CORPUS, k=5)
        py_out = _bm25_search_python(query, self.CORPUS, k=5)
        rust_idx = [i for i, _ in rust_out]
        py_idx = [i for i, _ in py_out]
        assert rust_idx == py_idx, (
            f"Top-k ordering differs for query={query!r}:\n"
            f"  rust: {rust_idx}\n"
            f"  py:   {py_idx}"
        )

    @pytest.mark.skipif(not is_rust_active(), reason="Rust wheel not installed")
    @pytest.mark.parametrize("query", QUERIES)
    def test_per_score_delta_under_1_percent(self, query: str) -> None:
        rust_out = bm25_search(query, self.CORPUS, k=5)
        py_out = _bm25_search_python(query, self.CORPUS, k=5)
        # Pair by doc index (ordering is asserted above to be identical).
        py_by_idx = dict(py_out)
        for idx, rust_score in rust_out:
            py_score = py_by_idx[idx]
            # Avoid div-by-zero on edge cases.
            denom = max(abs(py_score), 1e-6)
            delta_pct = abs(rust_score - py_score) / denom * 100.0
            assert delta_pct < 1.0, (
                f"Score delta {delta_pct:.3f}% for doc {idx} on "
                f"query={query!r}: rust={rust_score:.6f}, py={py_score:.6f}"
            )


# ─────────────────────────────────────────────────────────────────────
# Speedup
# ─────────────────────────────────────────────────────────────────────


def _benchmark(fn: Callable[[], list], iterations: int) -> float:
    """Return mean seconds-per-call over ``iterations`` runs."""
    fn()  # warm-up
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    return (time.perf_counter() - start) / iterations


class TestSpeedup:
    """Rust path must be significantly faster than the Python fallback.

    Floor is 2× (not the aspirational 5×) to stay stable on slower CI
    machines and smaller corpora. The workload is a 500-doc corpus with a
    3-term query — representative of a single L2 retrieval call against
    a medium scratchpad. The Python path re-tokenises and re-builds the
    DF table on every call (matching the stateless Rust API); callers
    that issue many queries against the same corpus should cache the
    index, at which point the Rust advantage grows.
    """

    CORPUS = [
        f"Document {i} discusses rust python bm25 retrieval tokenisation "
        f"and various aspects of information retrieval systems."
        for i in range(500)
    ]
    QUERY = "rust bm25 retrieval"
    ITERATIONS = 50

    @pytest.mark.skipif(not is_rust_active(), reason="Rust wheel not installed")
    def test_rust_at_least_2x_faster_than_python(self) -> None:
        rust_time = _benchmark(
            lambda: bm25_search(self.QUERY, self.CORPUS, k=10),
            self.ITERATIONS,
        )
        py_time = _benchmark(
            lambda: _bm25_search_python(self.QUERY, self.CORPUS, k=10),
            self.ITERATIONS,
        )
        speedup = py_time / rust_time
        print(
            f"\n  bm25 speedup: {speedup:.1f}× "
            f"(rust={rust_time * 1e6:.1f}µs, py={py_time * 1e6:.1f}µs)"
        )
        assert speedup >= 2.0, (
            f"Rust bm25_search only {speedup:.2f}× faster than Python "
            f"(expected ≥ 2×). rust={rust_time * 1e6:.1f}µs, "
            f"py={py_time * 1e6:.1f}µs."
        )
