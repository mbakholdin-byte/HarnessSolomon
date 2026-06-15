"""Phase 3: Privacy-aware embedder wrapper.

Wraps any ``Embedder`` and runs ``harness.redaction.redact`` on
the input text BEFORE embedding. This is defence in depth: even
if a future ``UnifiedMemory.write`` caller forgets to redact
sensitive content, the vector never carries PII or secrets.

The wrapper is itself an ``Embedder`` (Protocol), so it's a
drop-in replacement.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np

from harness.redaction import redact

logger = logging.getLogger(__name__)


class PrivacyAwareEmbedder:
    """Embedder wrapper that redacts input text before embedding.

    Args:
        inner:     The wrapped ``Embedder`` (e.g. ``OnnxEmbedder``).
        categories: Optional pattern set (forwarded to ``redact``).
    """

    def __init__(self, inner: Any, *, categories: set[str] | None = None) -> None:
        self._inner = inner
        self._categories = categories

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        redacted = [redact(t, categories=self._categories) for t in texts]
        return await self._inner.embed_documents(redacted)

    async def embed_query(self, text: str) -> np.ndarray:
        redacted = redact(text, categories=self._categories)
        return await self._inner.embed_query(redacted)
