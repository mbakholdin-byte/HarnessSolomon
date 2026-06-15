"""Tests for Model catalog (Шаг 5).

Per TDD (RED → GREEN → REFACTOR): tests first, then minimal implementation.

Endpoints under test:
  GET /api/models — returns list of models with availability based on env vars
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.db import sqlite as db_sqlite
from harness.server.llm.models import MODELS, get_model, list_models


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncClient:
    """Test client with isolated data dir."""
    data_dir = tmp_path / "harness-data"
    monkeypatch.setattr(settings, "session_dir", data_dir / "sessions")
    monkeypatch.setattr(settings, "db_path", data_dir / "harness.db")
    db_sqlite._db_initialized = False

    # Make sure no provider env vars leak in
    for k in ("MINIMAX_API_KEY", "ZHIPUAI_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# === Pure catalog tests ===

def test_models_catalog_has_four_models() -> None:
    """MODELS contains exactly 4 entries (Phase 3 added qwen3:8b T1)."""
    assert len(MODELS) == 4
    ids = {m["id"] for m in MODELS}
    assert ids == {"qwen3:8b", "MiniMax-M2.7", "glm-4.7", "moonshot-v1-128k"}


def test_get_model_known_id() -> None:
    """get_model returns spec for known id with correct provider."""
    spec = get_model("MiniMax-M2.7")
    assert spec is not None
    assert spec.id == "MiniMax-M2.7"
    assert spec.provider == "minimax"
    assert spec.tier == "T3"
    assert spec.ctx == 200000
    assert spec.env == "MINIMAX_API_KEY"
    assert spec.pricing_input == 0.30
    assert spec.pricing_output == 0.60


def test_get_model_unknown_returns_none() -> None:
    """Unknown id returns None."""
    assert get_model("nonexistent-model-xyz") is None
    assert get_model("") is None


def test_list_models_all_unavailable_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """All models have available=False when env vars are empty."""
    for k in (
        "MINIMAX_API_KEY", "ZHIPUAI_API_KEY", "MOONSHOT_API_KEY", "OLLAMA_HOST",
    ):
        monkeypatch.delenv(k, raising=False)

    models = list_models()
    assert len(models) == 4
    assert all(m.available is False for m in models)


def test_list_models_availability_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """If env var is set, model.available becomes True."""
    monkeypatch.setenv("ZHIPUAI_API_KEY", "test-key")
    for k in ("MINIMAX_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    models = list_models()
    by_id = {m.id: m for m in models}
    assert by_id["glm-4.7"].available is True
    assert by_id["MiniMax-M2.7"].available is False
    assert by_id["moonshot-v1-128k"].available is False


# === HTTP /api/models ===

async def test_get_models_endpoint(client: AsyncClient) -> None:
    """GET /api/models returns 200 + 4 models (Phase 3 added qwen3:8b)."""
    r = await client.get("/api/models")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 4

    # Every entry has expected keys
    for entry in data:
        assert "id" in entry
        assert "provider" in entry
        assert "tier" in entry
        assert "context" in entry
        assert "available" in entry
        assert "pricing_input" in entry
        assert "pricing_output" in entry
        assert entry["available"] is False  # env vars are empty in tests


async def test_get_models_endpoint_includes_minimax(client: AsyncClient) -> None:
    """/api/models includes MiniMax-M2.7 with provider minimax."""
    r = await client.get("/api/models")
    data = r.json()
    by_id = {m["id"]: m for m in data}
    assert "MiniMax-M2.7" in by_id
    assert by_id["MiniMax-M2.7"]["provider"] == "minimax"
    assert by_id["MiniMax-M2.7"]["context"] == 200000
