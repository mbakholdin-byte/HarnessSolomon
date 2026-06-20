"""Tests for ``GET /api/v1/capabilities`` (Phase 1.6, Step 2).

Covers:
  - returns 200 without auth (always public)
  - response schema is correct (server_version, auth_required, scopes_available, endpoints)
  - all 6 scopes listed with descriptions
  - endpoints are picked up from the mounted /api/v1/agents router
  - legacy /api/* routes are NOT in the endpoints list (they're open in Phase 1.6)
  - the /api/v1/capabilities endpoint itself is NOT in the endpoints list
  - auth_required reflects ``settings.auth_required`` at request time

We use ``httpx.AsyncClient`` with ``ASGITransport`` (not ``TestClient``)
because the latter triggers lifespan, which calls ``recover_running()``
on the JobStore and would race with our manual ``app.state`` setup.
For tests that need a real store (auth-enforcement tests), we attach
a fresh :class:`JobStore` to ``app.state`` directly — matching the
pattern from ``test_agents_api.py``.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.auth.tokens import TokenStore


def _make_app_with_state(*, auth_store: TokenStore | None = None) -> FastAPI:
    """Build a fresh app and pre-populate ``app.state`` for the routes we hit.

    The capabilities endpoint itself doesn't need any state — it just
    reads ``app.version`` and the mounted routes. The agents jobs
    routes need a ``job_store`` and a ``token_store`` (or auth off);
    we wire those up here so a single helper covers both classes of
    tests.
    """
    app = create_app()
    app.state.auth_required = settings.auth_required
    # Always wire a real token store so the auth dep doesn't 503
    # before the scope check even runs. In dev mode (auth_required
    # False) the dep short-circuits and the store isn't queried.
    if auth_store is None:
        ts = TokenStore(settings.auth_db_path)
        # Skip init — it would create a schema; a fresh path
        # is fine because the only thing we need is the attribute
        # to be present. The deps will check `auth_required`
        # before any DB call.
        auth_store = ts
    app.state.token_store = auth_store
    # The agents_jobs routes need a JobStore. We construct a fresh
    # one against the tmp dir (set by ``isolated_settings``). JobStore
    # auto-initialises the schema on the first read/write, so no
    # explicit init() call is needed.
    from harness.agents.jobs import JobStore
    db_path = settings.db_path.parent / "agent-jobs.db"
    app.state.job_store = JobStore(db_path)
    # No merge_queue in unit tests — the agents/health route handles
    # that gracefully (returns empty queue_locks).
    app.state.merge_queue = None
    return app


class TestCapabilitiesEndpoint:
    async def test_returns_200_without_auth(
        self, isolated_settings: dict,
    ) -> None:
        """The capabilities endpoint is always public — no token needed."""
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        assert r.status_code == 200

    async def test_response_schema(
        self, isolated_settings: dict,
    ) -> None:
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        body = r.json()
        assert "server_version" in body
        assert "auth_required" in body
        assert "scopes_available" in body
        assert "endpoints" in body
        assert body["server_version"]  # non-empty
        assert isinstance(body["scopes_available"], list)
        assert isinstance(body["endpoints"], list)

    async def test_all_scopes_listed(
        self, isolated_settings: dict,
    ) -> None:
        """Phase 3 v1.4.0 added ``sessions.write``; Phase 4.11 v1.21.0 adds
        ``observability.read`` and ``elicitation.read``; Phase 7.3 v1.31.0 adds
        ``hooks.admin`` and ``plugins.admin`` (WI-01/02).
        """
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        body = r.json()
        names = {s["name"] for s in body["scopes_available"]}
        assert names == {
            "agents.read", "agents.write", "agents.pr",
            "memory.read", "memory.write",
            "sessions.read", "sessions.write",  # Phase 3 v1.4.0
            "observability.read",  # Phase 4.11 v1.21.0
            "elicitation.read",    # Phase 4.11 v1.21.0
            "elicitation.write",   # v1.0.0 security fix
            "webhooks.admin",      # Phase 4.13B v1.23.0
            "privacy.read",        # Phase 5.3 v1.25.0
            "privacy.write",       # Phase 5.3 v1.25.0
            "hooks.admin",         # Phase 7.3 v1.31.0 WI-01
            "plugins.admin",       # Phase 7.3 v1.31.0 WI-02
        }
        # Each scope has a non-empty description.
        for s in body["scopes_available"]:
            assert s["description"], f"scope {s['name']} has no description"

    async def test_endpoints_listed_for_v1_routes(
        self, isolated_settings: dict,
    ) -> None:
        """The /api/v1/agents/* routes (now scoped) should appear in endpoints."""
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        body = r.json()
        paths = {e["path"] for e in body["endpoints"]}
        # The agents routes are now scope-gated; should be in capabilities.
        assert "/api/v1/agents/jobs" in paths
        assert "/api/v1/agents/jobs/{job_id}" in paths
        assert "/api/v1/agents/health" in paths

    async def test_legacy_api_routes_excluded(
        self, isolated_settings: dict,
    ) -> None:
        """Legacy /api/* routes are open in Phase 1.6 — must not appear."""
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        body = r.json()
        paths = {e["path"] for e in body["endpoints"]}
        for legacy in ("/api/sessions", "/api/models", "/api/health"):
            assert legacy not in paths, f"{legacy} should not be in v1 capabilities"

    async def test_capabilities_self_excluded(
        self, isolated_settings: dict,
    ) -> None:
        """The capabilities endpoint itself is public, so no scopes = not listed."""
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        body = r.json()
        paths = {e["path"] for e in body["endpoints"]}
        assert "/api/v1/capabilities" not in paths

    async def test_auth_required_reflects_settings(
        self, isolated_settings: dict, monkeypatch,
    ) -> None:
        """The response says whether auth is on — clients use this to decide
        whether to send a token.
        """
        # isolated_settings sets auth_required=False; verify the response matches.
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        assert r.json()["auth_required"] is False

        # Now flip the setting and re-request.
        monkeypatch.setattr(settings, "auth_required", True)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        assert r.json()["auth_required"] is True

    async def test_agents_routes_actually_enforce_scope(
        self, isolated_settings: dict, make_token, monkeypatch, auth_store,
    ) -> None:
        """End-to-end: hit /api/v1/agents/jobs without a token and with
        a wrong-scope token, both should fail when auth is required.
        """
        from harness.server.auth.scopes import Scope as S
        # Turn auth ON for this test.
        monkeypatch.setattr(settings, "auth_required", True)
        # Mint a token that does NOT have agents.read.
        plaintext, _ = await make_token("no-read", {S.MEMORY_WRITE})

        app = _make_app_with_state(auth_store=auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            # No token: 401.
            r = await ac.get("/api/v1/agents/jobs?recent=1")
            assert r.status_code == 401
            # Wrong scope: 403.
            r = await ac.get(
                "/api/v1/agents/jobs?recent=1",
                headers={"Authorization": f"Bearer {plaintext}"},
            )
            assert r.status_code == 403
            assert "agents.read" in r.json()["detail"]

    async def test_agents_routes_open_in_dev_mode(
        self, isolated_settings: dict,
    ) -> None:
        """With auth_required=False (default in isolated_settings),
        the agents routes accept requests without a token. This is
        the 'dev mode' contract.
        """
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/agents/jobs?recent=0")
        # 200 with empty list (no jobs in fresh tmp).
        assert r.status_code == 200
        assert r.json() == []
