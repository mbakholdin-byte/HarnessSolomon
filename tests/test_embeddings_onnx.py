"""Phase 3: smoke tests for the OnnxEmbedder.

The real ``OnnxEmbedder`` requires a downloaded ONNX model + the
``onnxruntime`` + ``tokenizers`` deps. We test the surface that
doesn't need a model loaded (constructor + property accessors) and
a few behaviours that the runtime exercises before any ONNX call.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from harness.config import Settings
from harness.memory.embeddings import OnnxEmbedder
from harness.memory.embeddings.onnx_backend import (
    DOCUMENT_PREFIX,
    QUERY_PREFIX,
)


class TestOnnxEmbedderSurface:
    def test_dim_property(self) -> None:
        s = Settings(embedding_dim=384)
        e = OnnxEmbedder(s)
        assert e.dim == 384

    def test_model_id_combines_model_and_precision(self) -> None:
        s = Settings(
            embedding_model="intfloat/multilingual-e5-small",
            embedding_precision="int8",
        )
        e = OnnxEmbedder(s)
        assert e.model_id == "intfloat/multilingual-e5-small-int8@1"

    def test_model_id_fp32(self) -> None:
        s = Settings(embedding_precision="fp32")
        e = OnnxEmbedder(s)
        assert e.model_id.endswith("-fp32@1")

    def test_constructor_does_not_load_model(self) -> None:
        # Construction must not call out to disk or network.
        s = Settings()
        e = OnnxEmbedder(s)
        # Internal session/tokenizer are None until first embed call.
        assert e._session is None
        assert e._tokenizer is None

    def test_embed_documents_empty_input(self) -> None:
        s = Settings()
        e = OnnxEmbedder(s)
        import asyncio
        result = asyncio.run(e.embed_documents([]))
        assert result.shape == (0, s.embedding_dim)

    def test_missing_onnxruntime_raises_clear_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the onnxruntime import to fail.
        import builtins
        original_import = builtins.__import__
        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "onnxruntime":
                raise ImportError("simulated missing dep")
            return original_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", fake_import)

        s = Settings()
        e = OnnxEmbedder(s)
        # Loading the model should fail with a clear message.
        with pytest.raises(RuntimeError) as exc:
            e._ensure_loaded()
        assert "embeddings" in str(exc.value).lower()
        assert "pip install" in str(exc.value).lower()


class TestE5Prefixes:
    def test_query_prefix(self) -> None:
        assert QUERY_PREFIX == "query: "

    def test_document_prefix(self) -> None:
        assert DOCUMENT_PREFIX == "passage: "


class TestPrivacyAwareEmbedder:
    """Smoke tests for the privacy wrapper — comprehensive tests live
    in ``test_dense_retriever.py``."""

    def test_dim_and_model_id_delegate(self) -> None:
        from harness.memory.embeddings import PrivacyAwareEmbedder

        inner = MagicMock()
        inner.dim = 384
        inner.model_id = "x@1"
        wrapped = PrivacyAwareEmbedder(inner)
        assert wrapped.dim == 384
        assert wrapped.model_id == "x@1"
