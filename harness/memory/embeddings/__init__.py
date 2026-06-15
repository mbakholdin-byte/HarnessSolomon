"""Phase 3: ONNX-backed embedding adapters for ``UnifiedMemory``.

Public API:
    - ``Embedder`` (Protocol) — defined in :mod:`.base`
    - ``OnnxEmbedder`` — :class:`harness.config.Settings`-driven
      ONNX Runtime backend for ``intfloat/multilingual-e5-small``
    - ``PrivacyAwareEmbedder`` — wraps any embedder and redacts
      input text before embedding

The dense retriever and the unified-memory writer consume these.
"""
from __future__ import annotations

from harness.memory.embeddings.base import Embedder
from harness.memory.embeddings.onnx_backend import (
    DOCUMENT_PREFIX,
    QUERY_PREFIX,
    OnnxEmbedder,
)
from harness.memory.embeddings.privacy import PrivacyAwareEmbedder

__all__ = [
    "DOCUMENT_PREFIX",
    "Embedder",
    "OnnxEmbedder",
    "PrivacyAwareEmbedder",
    "QUERY_PREFIX",
]
