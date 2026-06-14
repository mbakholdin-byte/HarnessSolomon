"""Tests for the Phase 1.6 ``/api/v1/memory/*`` and ``/api/v1/sessions`` routes.

Covers:
  - GET  /api/v1/memory/search   (memory.read)
  - POST /api/v1/memory/notes    (memory.write)
  - GET  /api/v1/memory/stats    (memory.read)
  - GET  /api/v1/sessions        (sessions.read)
  - 401 without token (when auth_required=True)
  - 403 with wrong scope
  - 200 with right scope
  - cross-route scope combinations (memory.read cannot write notes;
    memory.write cannot search; etc.)
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
    """Build a fresh app with both store types wired on app.state."""
    app = create_app()
    app.state.auth_required = settings.auth_required  # False in tests
    app.state.token_store = auth_store
    from harness.agents.jobs import JobStore
    app.state.job_store = JobStore(settings.db_path.parent / "agent-jobs.db")
    app.state.merge_queue = None
    return app


def _bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


class TestMemorySearch:
    async def test_requires_auth_when_auth_required(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        """No token -> 401 when auth is required."""
        monkeypatch.setattr(settings, "auth_required", True)
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/memory/search?q=hello")
        assert r.status_code == 401

    async def test_returns_hits_for_search(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        """With memory.write we can add a note, then memory.read finds it."""
        monkeypatch.setattr(settings, "auth_required", True)
        # Mint a write+read token.
        plaintext, _ = await make_token(
            "search-test", {Scope.MEMORY_WRITE, Scope.MEMORY_READ},
        )
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            # Write a note.
            r = await ac.post(
                "/api/v1/memory/notes",
                json={"text": "the quick brown fox", "layer": "L2"},
                headers=_bearer(plaintext),
            )
            assert r.status_code == 201, r.text
            # Search for it.
            r = await ac.get(
                "/api/v1/memory/search?q=fox&k=5",
                headers=_bearer(plaintext),
            )
            assert r.status_code == 200
            body = r.json()
            assert body["query"] == "fox"
            assert body["k"] == 5
            assert len(body["hits"]) >= 1
            # The hit contains our text.
            assert any("fox" in h["text"] for h in body["hits"])

    async def test_wrong_scope_returns_403(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        # Token has only sessions.read — should be denied on memory search.
        plaintext, _ = await make_token(
            "wrong-scope", {Scope.SESSIONS_READ},
        )
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/memory/search?q=x",
                headers=_bearer(plaintext),
            )
        assert r.status_code == 403
        assert "memory.read" in r.json()["detail"]

    async def test_empty_query_returns_422(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("empty", {Scope.MEMORY_READ})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/memory/search?q=",
                headers=_bearer(plaintext),
            )
        assert r.status_code == 422


class TestMemoryNotes:
    async def test_write_requires_memory_write_scope(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        # read-only token: 403 on write.
        plaintext, _ = await make_token("read-only", {Scope.MEMORY_READ})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/memory/notes",
                json={"text": "hi"},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 403
        assert "memory.write" in r.json()["detail"]

    async def test_write_persists(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("writer", {Scope.MEMORY_WRITE})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/memory/notes",
                json={"text": "hello world", "layer": "L2", "tags": ["#greeting"]},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 201
        body = r.json()
        assert body["layer"] == "L2"
        assert body["source"] == "manual"
        # The user-supplied tag survived.
        assert "#greeting" in body["tags"]
        # The response carries the agent_id stamped in metadata.
        assert body["agent_id"] == "solomon"

    async def test_invalid_layer_returns_422(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("bad-layer", {Scope.MEMORY_WRITE})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/memory/notes",
                json={"text": "x", "layer": "L9"},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 422

    async def test_empty_text_returns_422(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("empty-text", {Scope.MEMORY_WRITE})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/memory/notes",
                json={"text": ""},
                headers=_bearer(plaintext),
            )
        assert r.status_code == 422


class TestMemoryStats:
    async def test_stats_returns_agent_id_and_layers(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("stats", {Scope.MEMORY_READ})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/memory/stats", headers=_bearer(plaintext),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"] == "solomon"
        assert "L1_hmem_entries" in body["layers"]
        assert body["layers"]["L2_mem0_available"] is True


class TestSessionsV1:
    async def test_requires_sessions_read(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("no-sessions", {Scope.MEMORY_READ})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/sessions?recent=5",
                headers=_bearer(plaintext),
            )
        assert r.status_code == 403
        assert "sessions.read" in r.json()["detail"]

    async def test_returns_empty_list_when_no_sessions(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("reader", {Scope.SESSIONS_READ})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/sessions?recent=5",
                headers=_bearer(plaintext),
            )
        assert r.status_code == 200
        assert r.json() == []


class TestScopeCombinations:
    """Cross-route scope matrix: verify that each token's effective
    capabilities are exactly the union of its scopes.
    """

    async def test_read_only_token_cannot_write(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token(
            "reader", {Scope.MEMORY_READ},
        )
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/memory/search?q=x",
                headers=_bearer(plaintext),
            )
            assert r.status_code == 200
            r = await ac.post(
                "/api/v1/memory/notes",
                json={"text": "blocked"},
                headers=_bearer(plaintext),
            )
            assert r.status_code == 403

    async def test_write_only_token_cannot_search(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token(
            "writer", {Scope.MEMORY_WRITE},
        )
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/memory/notes",
                json={"text": "ok"},
                headers=_bearer(plaintext),
            )
            assert r.status_code == 201
            r = await ac.get(
                "/api/v1/memory/search?q=x",
                headers=_bearer(plaintext),
            )
            assert r.status_code == 403

    async def test_agents_pr_only_has_no_routes(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        """``agents.pr`` is declared but no routes use it in Phase 1.6.

        The token is valid (auth passes) but every protected route
        403s because the scope is irrelevant to them. We verify on
        the agents/jobs route as a representative.
        """
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("pr-only", {Scope.AGENTS_PR})
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/agents/jobs?recent=1",
                headers=_bearer(plaintext),
            )
        assert r.status_code == 403
        assert "agents.read" in r.json()["detail"]

    async def test_bootstrap_admin_can_call_anything(
        self, isolated_settings: dict[str, Path], auth_store: TokenStore,
        make_token, monkeypatch,
    ) -> None:
        """A token with ALL_SCOPES is the bootstrap admin — can call
        every protected route. (Phase 1.6 doesn't have a POST
        /api/v1/agents/jobs yet, so we test the read-side ones.)
        """
        from harness.server.auth.scopes import ALL_SCOPES
        monkeypatch.setattr(settings, "auth_required", True)
        plaintext, _ = await make_token("admin", set(ALL_SCOPES))
        app = _make_app(auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/agents/jobs?recent=0",
                headers=_bearer(plaintext),
            )
            assert r.status_code == 200
            r = await ac.get(
                "/api/v1/memory/search?q=x",
                headers=_bearer(plaintext),
            )
            assert r.status_code == 200
            r = await ac.get(
                "/api/v1/sessions?recent=0",
                headers=_bearer(plaintext),
            )
            assert r.status_code == 200
            r = await ac.get(
                "/api/v1/memory/stats",
                headers=_bearer(plaintext),
            )
            assert r.status_code == 200
