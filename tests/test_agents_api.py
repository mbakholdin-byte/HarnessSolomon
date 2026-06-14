"""Tests for the /api/v1/agents/jobs HTTP routes (Phase 2.2, Step 4).

Covers:
  - GET /api/v1/agents/jobs/{id} returns 200 with a JobRecord-shaped JSON
  - GET /api/v1/agents/jobs/{id} returns 404 for unknown id
  - GET /api/v1/agents/jobs?recent=N returns up to N rows (newest first)
  - GET /api/v1/agents/health returns 200 with queue_locks + job_count
  - 503 if app.state.job_store is None
  - Lifespan starts even when LLM router init fails (dev mode)

We use ``httpx.AsyncClient`` with ``ASGITransport`` (NOT
``TestClient``) because the latter triggers lifespan, which calls
``recover_running()`` and would cancel the freshly-created job rows.
The async client hits the app without running lifespan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from harness.agents.jobs import JobStore
from harness.config import settings


# === Helpers ===

def _make_app_with_store(store: JobStore) -> object:
    """Build a FastAPI app and pre-populate ``app.state``.

    Phase 1.6: the agents_jobs routes now require ``agents.read``
    scope, which is enforced by a FastAPI dep that pulls the
    ``TokenStore`` from ``app.state``. In dev mode
    (``auth_required=False``, set by ``isolated_settings``) the
    dep short-circuits and the store is never queried — but the
    attribute still has to be present (the dep raises 503 if
    it's None when the check happens). We attach a real
    ``TokenStore`` so the dep has something to read.
    """
    from harness.server.app import create_app
    from harness.server.auth.tokens import TokenStore
    app = create_app()
    app.state.job_store = store
    app.state.token_store = TokenStore(settings.auth_db_path)
    app.state.auth_required = settings.auth_required  # False in tests
    app.state.merge_queue = None
    return app


async def _async_client(app):
    """Return an ``httpx.AsyncClient`` bound to the app (no lifespan)."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestAgentsAPI:
    async def test_get_job_returns_record(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """GET /api/v1/agents/jobs/{id} returns the JobRecord as JSON."""
        store = JobStore(isolated_settings["data"] / "agent-jobs.db")
        jid = await store.create(
            worktree_id="wt-1", model="m", prompt="x",
            pr_mode="draft", target_branch="main",
        )
        app = _make_app_with_store(store)
        async with await _async_client(app) as client:
            r = await client.get(f"/api/v1/agents/jobs/{jid}")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["id"] == jid
            assert body["worktree_id"] == "wt-1"
            assert body["status"] == "queued"
            # Phase 2.2: PR fields surfaced in JSON.
            assert body["pr_mode"] == "draft"
            assert body["target_branch"] == "main"
            assert body["pr_url"] is None
            assert body["pr_number"] is None

    async def test_get_job_unknown_returns_404(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = JobStore(isolated_settings["data"] / "agent-jobs.db")
        app = _make_app_with_store(store)
        async with await _async_client(app) as client:
            r = await client.get("/api/v1/agents/jobs/does-not-exist")
            assert r.status_code == 404
            assert "not found" in r.json()["detail"]

    async def test_list_jobs_recent(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = JobStore(isolated_settings["data"] / "agent-jobs.db")
        for i in range(5):
            await store.create(
                worktree_id=f"wt-{i}", model="m", prompt=f"p{i}",
            )
        app = _make_app_with_store(store)
        async with await _async_client(app) as client:
            r = await client.get("/api/v1/agents/jobs?recent=3")
            assert r.status_code == 200
            body = r.json()
            assert len(body) == 3
            # Newest first.
            assert body[0]["worktree_id"] == "wt-4"
            assert body[2]["worktree_id"] == "wt-2"

    async def test_list_jobs_recent_zero_returns_empty(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = JobStore(isolated_settings["data"] / "agent-jobs.db")
        await store.create(worktree_id="wt-0", model="m", prompt="p")
        app = _make_app_with_store(store)
        async with await _async_client(app) as client:
            r = await client.get("/api/v1/agents/jobs?recent=0")
            assert r.status_code == 200
            assert r.json() == []

    async def test_health_endpoint(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = JobStore(isolated_settings["data"] / "agent-jobs.db")
        app = _make_app_with_store(store)
        async with await _async_client(app) as client:
            r = await client.get("/api/v1/agents/health")
            assert r.status_code == 200
            body = r.json()
            # No merge_queue => empty locks dict.
            assert body["queue_locks"] == {}
            assert body["job_store_path"].endswith("agent-jobs.db")
            assert body["recent_job_count"] == 0

    async def test_503_when_store_missing(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """If app.state.job_store is None, the routes return 503."""
        from harness.server.app import create_app
        app = create_app()
        # DO NOT set app.state.job_store.
        async with await _async_client(app) as client:
            r = await client.get("/api/v1/agents/jobs/anything")
            assert r.status_code == 503
            assert "not initialised" in r.json()["detail"]


class TestLifespanSmoke:
    def test_lifespan_starts_without_llm_keys(
        self, isolated_settings: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lifespan should not crash when the LLM router can't init.

        The agents routes will return 503, but the rest of the
        server (sessions, chat) starts. This is the dev-mode
        contract: no API keys, no merge-queue, but the server
        still responds.
        """
        from harness.server.app import create_app
        from fastapi.testclient import TestClient
        app = create_app()
        # Override settings so db_path lives under tmp.
        monkeypatch.setattr(settings, "db_path", isolated_settings["db_path"])
        monkeypatch.setattr(settings, "session_dir", isolated_settings["session_dir"])
        with TestClient(app) as client:
            # sessions endpoint should be alive.
            r = client.get("/api/sessions")
            # The exact status code depends on the test environment,
            # but it must NOT be 5xx (server didn't crash).
            assert r.status_code < 500
            # agents routes return 503 (no LLM keys -> no merge queue).
            r = client.get("/api/v1/agents/health")
            # With no LLM keys, the merge_queue is None and the
            # route returns 503. If somehow the merge_queue is
            # built, 200 is also acceptable.
            assert r.status_code in (200, 503)
