"""Pure-Python BM25 retriever (Phase 1, Step 7).

BM25 (Best Matching 25) is a classic ranking function for
information retrieval. We use it as the **sparse** half of the
hybrid retriever; a real Qdrant-backed dense retriever can be
added later and combined with this one in the pipeline.

Implementation notes:
  - We tokenise on whitespace + lowercase (good enough for
    English / Russian / technical corpora; no stemming)
  - We compute BM25 with the standard k1=1.5, b=0.75 defaults
  - We return ``(Memory, score)`` tuples sorted by score desc
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Protocol

from harness.memory.schema import Memory

# BM25 hyper-parameters
_K1: float = 1.5
_B: float = 0.75

# Tokenisation: split on non-alphanumeric (keep CJK / Cyrillic as
# one character each — they survive any unicode regex match).
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokenise(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class Retriever(Protocol):
    """Protocol for any retriever the pipeline can use.

    Implementations: ``BM25Retriever`` (sparse), or a future
    Qdrant-backed dense retriever. The pipeline doesn't care
    which.
    """

    def retrieve(self, query: str, k: int) -> list[tuple[Memory, float]]:
        """Return up to ``k`` (Memory, score) tuples, score desc."""
        ...


class BM25Retriever:
    """Sparse BM25 retriever over an in-memory Memory corpus.

    Args:
        corpus: List of Memory records to search over. A new
                instance is built on each construction; the
                retriever is **stateless** w.r.t. updates (callers
                must construct a fresh one when the corpus
                changes).
    """

    def __init__(self, corpus: Iterable[Memory]) -> None:
        self._corpus: list[Memory] = list(corpus)
        self._docs_tokens: list[list[str]] = [
            _tokenise(m.content) for m in self._corpus
        ]
        self._doc_freqs: dict[str, int] = Counter()
        for tokens in self._docs_tokens:
            for term in set(tokens):
                self._doc_freqs[term] += 1
        self._avgdl: float = (
            sum(len(t) for t in self._docs_tokens) / max(len(self._docs_tokens), 1)
        )
        self._N: int = len(self._corpus)

    def retrieve(self, query: str, k: int) -> list[tuple[Memory, float]]:
        """Return up to ``k`` (Memory, score) tuples for the query.

        Returns an empty list when the query has no terms, when
        no document matches, or when ``k <= 0``.
        """
        if k <= 0 or not self._corpus:
            return []
        q_tokens = _tokenise(query)
        if not q_tokens:
            return []

        scores: list[tuple[int, float]] = []
        for idx, doc_tokens in enumerate(self._docs_tokens):
            if not doc_tokens:
                continue
            score = self._bm25_score(q_tokens, doc_tokens)
            if score > 0:
                scores.append((idx, score))

        if not scores:
            return []

        # Sort by score desc, then by doc index for stable order
        scores.sort(key=lambda x: (-x[1], x[0]))
        top = scores[:k]
        return [(self._corpus[i], s) for i, s in top]

    def _bm25_score(self, q_tokens: list[str], doc_tokens: list[str]) -> float:
        """Compute the BM25 score for one doc given the query terms."""
        dl = len(doc_tokens)
        tf: Counter[str] = Counter(doc_tokens)
        score = 0.0
        for term in q_tokens:
            if term not in tf:
                continue
            df = self._doc_freqs.get(term, 0)
            # IDF (BM25+ variant — never negative)
            idf = math.log(((self._N - df + 0.5) / (df + 0.5)) + 1.0)
            # Term-frequency saturation
            tf_norm = (tf[term] * (_K1 + 1)) / (
                tf[term] + _K1 * (1 - _B + _B * dl / self._avgdl)
            )
            score += idf * tf_norm
        return score


__all__ = ["BM25Retriever", "Retriever"]
