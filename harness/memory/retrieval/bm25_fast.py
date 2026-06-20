"""Phase 6.4 v1.29.0: BM25 search — Rust fast path.

Wraps the optional Rust extension ``harness_perf.bm25_search`` (in-memory
BM25 with k1=1.5, b=0.75) with a pure-Python fallback that produces
identical rankings. This is a *thin* function-level fast path for the
(query, list[str], k) shape; it does not replace the canonical
:class:`harness.memory.retrieval.bm25.BM25Retriever`, which remains the
class to use when callers already hold ``Memory`` objects.

Trust boundary:
    The Rust module is a leaf dependency — it does NOT import any
    ``harness.*`` code and operates purely on ``str`` / ``list[str]`` /
    ``list[tuple[int, float]]``. This wrapper is the only place that
    bridges into the harness package.

Fallback policy:
    On any ``ImportError`` (Rust wheel not built, wrong Python ABI,
    platform without a Rust toolchain) we fall back to an inline
    pure-Python BM25 that reproduces ``BM25Retriever._bm25_score``
    term-by-term. The ranking is identical (same hyper-parameters, same
    tokeniser, same tie-break) so callers see no behavioural change —
    only the latency profile differs.

Score parity:
    Per-score delta is < 1 % (f32 on Rust vs float64 on Python in the
    IDF/TF-norm products). Top-k ordering is identical on every corpus
    we have tested (see ``tests/perf/test_bm25_v129.py``).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

__all__ = ["bm25_search", "is_rust_active"]

# BM25 hyper-parameters. MUST match ``bm25::K1`` / ``bm25::B`` in
# ``harness-perf/src/bm25.rs`` and ``_K1`` / ``_B`` in
# ``harness/memory/retrieval/bm25.py`` — all three copies must agree or
# the Rust and Python paths diverge.
_K1: float = 1.5
_B: float = 0.75

# Tokeniser. MUST match ``bm25::tokenise`` and ``BM25Retriever._tokenise``:
# split on ``\w+`` (Unicode-aware), lowercase each token.
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokenise(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@lru_cache(maxsize=1)
def _rust_available() -> bool:
    """Return ``True`` iff the ``harness_perf`` Rust extension imports.

    Cached — the import probe runs at most once per process.
    """
    try:
        import harness_perf  # noqa: F401  (import side-effect only)
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def is_rust_active() -> bool:
    """Public probe: is the Rust fast path currently in use?

    Exposed for tests and observability.
    """
    return _rust_available()


def bm25_search(
    query: str,
    documents: list[str],
    k: int,
) -> list[tuple[int, float]]:
    """Rank ``documents`` by BM25 score against ``query``; return top-k.

    Args:
        query:     Natural-language query. Tokenised on ``\\w+`` and
                   lowercased (mirrors ``BM25Retriever._tokenise``).
        documents: Corpus of document strings. Indices in the result
                   refer to positions in this list.
        k:         Maximum number of results to return. ``k <= 0``
                   returns an empty list.

    Returns:
        List of ``(doc_index, score)`` tuples, sorted by score desc
        then doc_index asc (matches Python ``BM25Retriever.retrieve``
        ordering). Documents with score ``<= 0`` are dropped.

    Notes:
        * Empty ``query`` / empty ``documents`` / ``k <= 0`` → empty list.
        * Scores are ``float`` (f32 on the Rust path, f64 on fallback).
          Per-score delta < 1 %; top-k ordering is identical.
    """
    if k <= 0 or not documents or not query:
        return []

    if _rust_available():
        import harness_perf
        return harness_perf.bm25_search(query, list(documents), k)

    # ── Pure-Python fallback ─────────────────────────────────────────
    # Reproduces ``BM25Retriever._bm25_score`` term-by-term. Kept inline
    # (rather than delegating to the class) so this module has zero
    # coupling to the Memory schema — the function-level API stays a leaf.
    return _bm25_search_python(query, documents, k)


def _bm25_search_python(
    query: str,
    documents: list[str],
    k: int,
) -> list[tuple[int, float]]:
    """Pure-Python BM25 ranking — fallback for the Rust fast path.

    Identical formula to :meth:`BM25Retriever._bm25_score`:
        idf     = log(((N - df + 0.5) / (df + 0.5)) + 1.0)
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
        score   = Σ_term idf * tf_norm
    """
    q_tokens = _tokenise(query)
    if not q_tokens:
        return []

    docs_tokens: list[list[str]] = [_tokenise(d) for d in documents]
    n_docs = len(docs_tokens)
    doc_freqs: Counter[str] = Counter()
    for tokens in docs_tokens:
        for term in set(tokens):
            doc_freqs[term] += 1
    avgdl = sum(len(t) for t in docs_tokens) / max(n_docs, 1)

    scored: list[tuple[int, float]] = []
    for idx, tokens in enumerate(docs_tokens):
        if not tokens:
            continue
        tf: Counter[str] = Counter(tokens)
        dl = len(tokens)
        score = 0.0
        for term in q_tokens:
            if term not in tf:
                continue
            df = doc_freqs.get(term, 0)
            idf = math.log(((n_docs - df + 0.5) / (df + 0.5)) + 1.0)
            tf_norm = (tf[term] * (_K1 + 1)) / (
                tf[term] + _K1 * (1 - _B + _B * dl / avgdl)
            )
            score += idf * tf_norm
        if score > 0:
            scored.append((idx, score))

    if not scored:
        return []

    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:k]
