"""Phase 3: Embedder protocol.

The Protocol here is the contract that ``DenseRetriever`` and
``UnifiedMemory.write`` rely on. The concrete implementation
(:class:`OnnxEmbedder`) is in ``onnx_backend.py``; the privacy
wrapper is in ``privacy.py``.

The embedder is intentionally minimal — just two methods that
take lists of strings and return vectors. ``embed_query`` and
``embed_documents`` are separate so the backend can apply the
asymmetric prefix (BGE/E5 require a different prefix for the
query side than for documents).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Embedder(Protocol):
    """A text-to-vector embedder.

    The two methods differ ONLY in the prefix applied (for asymmetric
    models like BGE / E5). All other postprocessing (mean pooling,
    L2 normalisation) is identical between the two.
    """

    @property
    def dim(self) -> int:
        """Output vector dimension (e.g. 384 for multilingual-e5-small)."""
        ...

    @property
    def model_id(self) -> str:
        """Identifier of the loaded model (e.g. 'multilingual-e5-small-int8@1')."""
        ...

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of documents. Shape: (N, dim), L2-normalised.

        For E5/BGE: no prefix is added to each text.
        """
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a single query. Shape: (dim,), L2-normalised.

        For E5: each text is prefixed with ``"query: "``.
        For BGE: each text is prefixed with
        ``"Represent this sentence for searching relevant passages: "``.
        """
        ...
