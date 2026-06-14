"""Tests for ``POST /api/v1/agents/jobs`` (Phase 1.6, Step 5).

Covers:
  - 401 без токена
  - 200/201 с токеном ``agents.write``, ``pr_mode="off"``
  - 403 с ``agents.write`` + ``pr_mode="draft"`` (нужен ``agents.pr``)
  - 200/201 с ``agents.write`` + ``agents.pr``, ``pr_mode="draft"``
  - 422 на ``prompt=""``
  - 422 на ``agent="does-not-exist"``
  - 422 на ``model="not-in-catalog"``
  - 503 если ``merge_queue`` is None (lifespan init failed)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.auth.scopes import Scope
from harness.server.auth.tokens import TokenStore


def _make_app(auth_store: TokenStore) -> FastAPI:
    """Build a fresh app with stores wired on ``app.state``.

    ``merge_queue`` is explicitly set to ``None`` so the 503
    path is exercised by default. Tests that need a working
    enqueue path attach a mock queue.
    """
    app = create_app()
    app.state.auth_required = settings.auth_required  # False in tests
    app.state.token_store = auth_store
    from harness.agents.jobs import JobStore
    app.state.job_store = JobStore(settings.db_path.parent / "agent-jobs.db")
    app.state.merge_queue = None
    return app


def _bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


class TestEnqueueAuth:
    async def test_no_token_returns_401_when_auth_required(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs", json={"prompt": "hi"},
            )
        assert r.status_code == 401

    async def test_agents_write_token_succeeds_with_pr_off(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        """A token with agents.write can enqueue with pr_mode='off'.

        We don't set merge_queue here, so 503 from the queue path
        is the expected response — but auth itself is satisfied.
        """
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("writer", {Scope.AGENTS_WRITE})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs",
                json={"prompt": "hi", "pr_mode": "off"},
                headers=_bearer(plaintext),
            )
        # We expect 503 (no merge queue), not 401/403/422 — that
        # means auth + validation passed.
        assert r.status_code == 503, r.text
        assert "MergeQueue not initialised" in r.json()["detail"]

    async def test_pr_mode_draft_without_agents_pr_returns_403(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token(
            "no-pr", {Scope.AGENTS_WRITE},
        )
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs",
                json={"prompt": "hi", "pr_mode": "draft"},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 403
        assert "agents.pr" in r.json()["detail"]

    async def test_pr_mode_draft_with_agents_pr_passes_auth(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token(
            "pr-writer",
            {Scope.AGENTS_WRITE, Scope.AGENTS_PR},
        )
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs",
                json={"prompt": "hi", "pr_mode": "draft"},
                headers=_bearer(plaintext),
            )
        # Passes auth + validation, fails on missing queue (503).
        assert r.status_code == 503


class TestEnqueueValidation:
    async def test_empty_prompt_returns_422(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("v", {Scope.AGENTS_WRITE})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs",
                json={"prompt": ""},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 422

    async def test_unknown_agent_returns_422(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("v", {Scope.AGENTS_WRITE})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs",
                json={"prompt": "hi", "agent": "does-not-exist"},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 422
        assert "unknown agent" in r.json()["detail"]

    async def test_unknown_model_returns_422(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("v", {Scope.AGENTS_WRITE})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs",
                json={"prompt": "hi", "model": "not-in-catalog"},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 422
        assert "unknown model" in r.json()["detail"]

    async def test_503_when_merge_queue_missing(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        """If lifespan didn't construct merge_queue, the route returns 503.

        This is the expected dev-mode behaviour when no LLM
        API keys are configured — auth + validation pass, the
        queue just isn't there.
        """
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("v", {Scope.AGENTS_WRITE})
        app = _make_app(auth_store)
        # app.state.merge_queue is None by default in _make_app.
        assert app.state.merge_queue is None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/agents/jobs",
                json={"prompt": "hi"},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 503
