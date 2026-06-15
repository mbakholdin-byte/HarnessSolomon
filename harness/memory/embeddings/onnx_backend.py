"""Phase 3: ONNX-backed embedder for multilingual-e5-small (or compatible).

The embedder is lazy: the ONNX model + tokenizer are loaded on the
first call to :meth:`embed_documents` or :meth:`embed_query`. The
download happens at first use (not at import time) and falls back
to the cached ``all-MiniLM-L6-v2`` model that chromadb previously
downloaded to ``C:/Users/<user>/.cache/chroma/onnx_models/``.

Model:
    - ``intfloat/multilingual-e5-small`` (default)
    - 118M params, 384 dim, ~120MB on disk
    - ONNX source: ``Xenova/multilingual-e5-small-onnx`` (HF Hub)
    - Multilingual: RU + EN + 100+ languages

Asymmetric prefixes (E5):
    - Query:   ``"query: "``
    - Document: ``"passage: "``

Postprocessing:
    - Mean-pooling over the sequence dimension (with attention-mask
      weighting)
    - L2-normalise the result

Threading: ``onnxruntime.InferenceSession`` is thread-safe but
``tokenizers`` is not. We serialise calls via an ``asyncio.Lock``
so a single instance is safe under concurrent asyncio use.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np

from harness.config import Settings

logger = logging.getLogger(__name__)


# E5 prefixes — public so the ``HybridRetriever`` tests can verify.
QUERY_PREFIX = "query: "
DOCUMENT_PREFIX = "passage: "


class OnnxEmbedder:
    """Multilingual E5-small embedder via ONNX Runtime.

    Args:
        settings: The harness ``Settings`` instance. The embedder
                  reads ``embeddings_dir``, ``embedding_model``, and
                  ``embedding_precision``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model_id = settings.embedding_model
        self._precision = settings.embedding_precision
        self._cache_dir = settings.embeddings_dir
        self._dim = settings.embedding_dim
        # Lazy-loaded.
        self._session: Any = None
        self._tokenizer: Any = None
        self._lock = asyncio.Lock()

    # === Public properties ===

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_id(self) -> str:
        return f"{self._model_id}-{self._precision}@1"

    # === Lazy load ===

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        # Try to import onnxruntime and tokenizers. If either is
        # missing, the operator hasn't installed the ``embeddings``
        # extra. We raise a clear error pointing at the install
        # command rather than a confusing ImportError downstream.
        try:
            import onnxruntime  # type: ignore[import-untyped]
        except ImportError as e:
            raise RuntimeError(
                "OnnxEmbedder requires the 'embeddings' extra. "
                "Install with: pip install -e '.[embeddings]'. "
                f"Original error: {e}"
            ) from e
        try:
            from tokenizers import Tokenizer  # type: ignore[import-untyped]
        except ImportError as e:
            raise RuntimeError(
                "OnnxEmbedder requires the 'tokenizers' package "
                "(transitive via litellm). If you installed via "
                "pip install -e '.[embeddings]' and still see this, "
                "the venv may need rebuilding. "
                f"Original error: {e}"
            ) from e
        # Find the model files. The ``Xenova/multilingual-e5-small-onnx``
        # HF Hub repo contains model.onnx + tokenizer.json.
        model_path = self._find_model_file()
        tokenizer_path = self._cache_dir / "tokenizer.json"
        if not model_path.exists() or not tokenizer_path.exists():
            raise FileNotFoundError(
                f"ONNX model not found at {model_path} (or tokenizer at "
                f"{tokenizer_path}). Run ``harness embeddings download`` "
                f"or set ``HARNESS_EMBEDDINGS_DIR`` to a directory "
                f"containing the files. Pre-cached fallback: "
                f"``C:/Users/<user>/.cache/chroma/onnx_models/"
                f"all-MiniLM-L6-v2/`` (different model — wrong dim)."
            )
        # Configure session options for CPU.
        sess_options = onnxruntime.SessionOptions()
        sess_options.graph_optimization_level = (
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        self._session = onnxruntime.InferenceSession(
            str(model_path), sess_options, providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        logger.info(
            "OnnxEmbedder loaded: model=%s, dim=%d, precision=%s",
            self.model_id, self._dim, self._precision,
        )

    def _find_model_file(self) -> Path:
        """Locate the model.onnx file. Search order:
        1. ``<embeddings_dir>/model.onnx`` (operator-installed)
        2. ``<embeddings_dir>/model_int8.onnx`` (INT8 quantised)
        """
        primary = self._cache_dir / "model.onnx"
        if primary.exists():
            return primary
        if self._precision == "int8":
            int8 = self._cache_dir / "model_int8.onnx"
            if int8.exists():
                return int8
        # Fallback: legacy chroma cache (different model — but the
        # dim won't match 384, so the test will catch it).
        import os
        user_cache = Path(os.path.expanduser("~")) / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2" / "model.onnx"
        if user_cache.exists():
            logger.warning(
                "OnnxEmbedder: using chroma's cached all-MiniLM-L6-v2 as "
                "fallback (dim will not match the configured 384)"
            )
            return user_cache
        return primary  # let the caller raise the not-found error

    # === Embedding ===

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of documents. No prefix is added.

        Returns ``(N, dim)`` float32 array, L2-normalised.
        """
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        async with self._lock:
            return await asyncio.to_thread(
                self._embed_batch, [DOCUMENT_PREFIX + t for t in texts],
            )

    async def embed_query(self, text: str) -> np.ndarray:
        """Embed a single query. Adds the E5 ``"query: "`` prefix.

        Returns ``(dim,)`` float32 array, L2-normalised.
        """
        async with self._lock:
            result = await asyncio.to_thread(
                self._embed_batch, [QUERY_PREFIX + text],
            )
            return result[0]

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        """Tokenise, run ONNX session, mean-pool, L2-normalise.

        Pure function (modulo ONNX session) — safe to call from
        ``asyncio.to_thread`` so we don't block the event loop.
        """
        self._ensure_loaded()
        import numpy as np
        # Tokenise.
        encodings = self._tokenizer.encode_batch(texts)
        max_len = max(len(e.ids) for e in encodings)
        ids = np.zeros(
            (len(texts), max_len), dtype=np.int64,
        )
        mask = np.zeros(
            (len(texts), max_len), dtype=np.int64,
        )
        for i, e in enumerate(encodings):
            ids[i, : len(e.ids)] = e.ids
            mask[i, : len(e.ids)] = e.attention_mask
        # ONNX forward. Standard E5 export has 2 inputs: input_ids +
        # attention_mask; output is last_hidden_state (batch, seq, dim).
        outputs = self._session.run(
            None, {"input_ids": ids, "attention_mask": mask},
        )
        last_hidden = outputs[0]  # (B, T, dim)
        # Mean-pool with attention-mask weighting.
        mask_f = mask.astype(np.float32)[:, :, None]
        summed = (last_hidden * mask_f).sum(axis=1)
        counts = mask_f.sum(axis=1).clip(min=1.0)
        pooled = summed / counts
        # L2-normalise.
        norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
        normalised = pooled / norms
        return normalised.astype(np.float32)
