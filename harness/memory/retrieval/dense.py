"""Phase 3: Dense retriever (cosine over ONNX embeddings).

The retriever matches the ``Retriever`` Protocol from
:mod:`harness.memory.retrieval.bm25`, so the existing
``RetrievalPipeline`` can swap BM25 for dense (or combine via
``HybridRetriever``) without code changes.

Vectors must be L2-normalised (the ``OnnxEmbedder`` guarantees
this), so the dot product is equivalent to cosine similarity.

**Versioning:** vectors whose ``metadata.embedding_version`` doesn't
match the embedder's ``model_id`` are excluded from dense retrieval.
The BM25 path still finds them, so no information is lost — the
operator can re-embed at their leisure.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from harness.memory.embeddings import Embedder
from harness.memory.retrieval.versioning import EMBEDDING_MODEL_VERSION
from harness.memory.schema import Memory

logger = logging.getLogger(__name__)


class DenseRetriever:
    """Cosine-similarity retriever over an L2-normalised corpus.

    Args:
        corpus:    List of ``Memory`` records to search.
        embedder:  The ``Embedder`` (typically ``OnnxEmbedder`` or
                   ``PrivacyAwareEmbedder``). Pre-computes the
                   corpus embeddings on construction.
    """

    def __init__(self, corpus: list[Memory], embedder: Embedder) -> None:
        self._embedder = embedder
        self._version = embedder.model_id
        # Filter the corpus to entries with a current-version
        # embedding. Older vectors fall back to BM25 only.
        kept: list[Memory] = []
        for m in corpus:
            meta = m.metadata or {}
            v = meta.get("embedding_version")
            emb = meta.get("embedding")
            if v == self._version and isinstance(emb, list) and emb:
                kept.append(m)
        self._corpus = kept
        # If the kept memories carry pre-computed vectors in their
        # metadata, build the matrix from those (no re-embed). The
        # ``metadata.embedding`` list is the canonical store; the
        # embedder is consulted only for query-time ``embed_query``.
        if kept:
            vectors: list[np.ndarray] = []
            for m in kept:
                emb = m.metadata.get("embedding")
                arr = np.asarray(emb, dtype=np.float32)
                # Re-normalise defensively in case the stored vector
                # was L2-normalised at write time but has since been
                # serialised+deserialised (small float drift).
                n = np.linalg.norm(arr)
                if n > 0:
                    arr = arr / n
                vectors.append(arr)
            self._matrix = np.stack(vectors).astype(np.float32)
        else:
            self._matrix = np.zeros((0, embedder.dim), dtype=np.float32)

    async def retrieve(
        self, query: str, k: int = 5,
    ) -> list[tuple[Memory, float]]:
        """Return the top-k ``(Memory, score)`` pairs by cosine similarity.

        Score is in ``[0, 1]`` because vectors are L2-normalised and
        the dot product of two unit vectors equals their cosine
        similarity (which lies in ``[-1, 1]``; for similar texts
        the value is positive).

        Returns an empty list if the corpus is empty.
        """
        if not self._corpus or self._matrix.shape[0] == 0:
            return []
        # Embed the query (prefixed with "query: ").
        q_vec = await self._embedder.embed_query(query)
        # Cosine = dot product (L2-normalised vectors).
        scores = self._matrix @ q_vec  # (N,)
        # Top-k (no partial sort needed for small corpora).
        k = min(k, len(self._corpus))
        # argpartition is O(N) and we slice the top-k.
        if k >= len(self._corpus):
            order = np.argsort(-scores)
        else:
            order = np.argpartition(-scores, k - 1)[:k]
            order = order[np.argsort(-scores[order])]
        return [
            (self._corpus[int(i)], float(scores[int(i)]))
            for i in order
        ]
