"""Tests for the Phase 1.6 FastAPI auth dependencies.

Covers:
  - ``get_current_token``:
      * missing Authorization header -> 401
      * malformed header (no Bearer, no space, etc.) -> 401
      * wrong token -> 401
      * valid token -> TokenRecord returned
      * revoked token -> 401 (same message as 'not found')
      * auth_required=False -> returns None (open mode)
  - ``require_scope``:
      * no scopes required + any token -> 200
      * required scope in token -> 200
      * required scope missing -> 403 with 'missing required scope'
      * multiple required (ANY match) -> 200 if at least one matches
      * auth_required=False -> bypasses check (200)
      * 401 (no token) bubbles up unchanged
"""
from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.auth.deps import get_current_token, require_scope
from harness.server.auth.scopes import Scope
from harness.server.auth.tokens import TokenRecord, TokenStore


# === Test app factory ===

def _build_test_app(
    *, auth_store: TokenStore | None, auth_required: bool = True,
) -> FastAPI:
    """Build a FastAPI app with a few test routes covering the deps.

    We use a separate app (not the main one) so the test routes
    can declare their own dependency graph. The main app stays
    untouched, and the auth deps are tested in isolation here.

    The ``auth_store`` is stashed on ``app.state`` so the
    ``get_token_store`` dependency can find it (mimicking what
    the lifespan handler does in production). When
    ``auth_required=False``, the deps short-circuit and the
    store isn't actually queried.
    """
    app = FastAPI()
    app.state.auth_required = auth_required
    app.state.token_store = auth_store

    @app.get("/_test/_any_token")
    async def any_token(token=Depends(get_current_token)):
        return {"token": token.label if token else None}

    @app.get("/_test/_needs_read")
    async def needs_read(token=Depends(require_scope(Scope.AGENTS_READ))):
        return {"ok": True, "label": token.label if token else None}

    @app.get("/_test/_needs_read_or_write")
    async def needs_read_or_write(
        token=Depends(require_scope(Scope.AGENTS_READ, Scope.AGENTS_WRITE)),
    ):
        return {"ok": True}

    return app


def _bearer(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.fixture
def test_app_factory(auth_store: TokenStore):
    """Return a function that builds a test app with the given auth mode.

    Each test calls ``test_app_factory(auth_required=True)`` (or
    False) and gets a fresh app with the auth_store pre-wired on
    app.state. Centralising this avoids the 503 we hit when the
    store is missing.
    """
    def _factory(*, auth_required: bool = True) -> FastAPI:
        return _build_test_app(
            auth_store=auth_store, auth_required=auth_required,
        )
    return _factory


# === get_current_token ===

class TestGetCurrentToken:
    async def test_missing_header_returns_401(
        self, isolated_settings: dict, test_app_factory,
    ) -> None:
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/_test/_any_token")
        assert r.status_code == 401
        assert "missing Authorization" in r.json()["detail"]
        assert r.headers.get("www-authenticate", "").lower() == "bearer"

    async def test_malformed_header_no_bearer_returns_401(
        self, isolated_settings: dict, test_app_factory,
    ) -> None:
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_any_token",
                headers={"Authorization": "Token abc"},
            )
        assert r.status_code == 401
        assert "invalid Authorization" in r.json()["detail"]

    async def test_malformed_header_no_space_returns_401(
        self, isolated_settings: dict, test_app_factory,
    ) -> None:
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_any_token",
                headers={"Authorization": "Bearer"},
            )
        assert r.status_code == 401
        assert "invalid Authorization" in r.json()["detail"]

    async def test_wrong_token_returns_401(
        self, isolated_settings: dict, test_app_factory,
    ) -> None:
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_any_token",
                headers={"Authorization": "Bearer not-a-real-token"},
            )
        assert r.status_code == 401
        assert "invalid or revoked" in r.json()["detail"]

    async def test_valid_token_returns_record(
        self, isolated_settings: dict, make_token, test_app_factory,
    ) -> None:
        plaintext, _ = await make_token("tester", {Scope.AGENTS_READ})
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_any_token", headers=_bearer(plaintext),
            )
        assert r.status_code == 200
        assert r.json()["token"] == "tester"

    async def test_revoked_token_returns_401(
        self, isolated_settings: dict, make_token, auth_store: TokenStore,
        test_app_factory,
    ) -> None:
        plaintext, record = await make_token(
            "will-revoke", {Scope.AGENTS_READ},
        )
        await auth_store.revoke(record.token_hash)
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_any_token", headers=_bearer(plaintext),
            )
        assert r.status_code == 401
        # Same message as 'not found' — we don't leak the revoke state.
        assert "invalid or revoked" in r.json()["detail"]

    async def test_auth_required_false_returns_none(
        self, isolated_settings: dict, test_app_factory,
    ) -> None:
        """When ``auth_required=False`` on app.state, the dep returns None."""
        app = test_app_factory(auth_required=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/_test/_any_token")
        assert r.status_code == 200
        assert r.json()["token"] is None

    async def test_case_insensitive_bearer_scheme(
        self, isolated_settings: dict, make_token, test_app_factory,
    ) -> None:
        plaintext, _ = await make_token("case-test", {Scope.AGENTS_READ})
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_any_token",
                headers={"Authorization": f"bearer {plaintext}"},
            )
        assert r.status_code == 200


# === require_scope ===

class TestRequireScope:
    async def test_required_scope_present_returns_200(
        self, isolated_settings: dict, make_token, test_app_factory,
    ) -> None:
        plaintext, _ = await make_token("read-ok", {Scope.AGENTS_READ})
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_needs_read", headers=_bearer(plaintext),
            )
        assert r.status_code == 200

    async def test_required_scope_missing_returns_403(
        self, isolated_settings: dict, make_token, test_app_factory,
    ) -> None:
        plaintext, _ = await make_token("wrong-scopes", {Scope.MEMORY_WRITE})
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_needs_read", headers=_bearer(plaintext),
            )
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert "missing required scope" in detail
        assert "agents.read" in detail
        assert "memory.write" in detail  # shows what we DO have

    async def test_any_of_required_scopes_satisfies(
        self, isolated_settings: dict, make_token, test_app_factory,
    ) -> None:
        plaintext, _ = await make_token("write-only", {Scope.AGENTS_WRITE})
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/_test/_needs_read_or_write", headers=_bearer(plaintext),
            )
        assert r.status_code == 200

    async def test_no_token_header_bubbles_401(
        self, isolated_settings: dict, test_app_factory,
    ) -> None:
        app = test_app_factory()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/_test/_needs_read")
        assert r.status_code == 401

    async def test_auth_required_false_bypasses_scope_check(
        self, isolated_settings: dict, test_app_factory,
    ) -> None:
        """When open mode is on, ``require_scope`` passes through."""
        app = test_app_factory(auth_required=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/_test/_needs_read")
        assert r.status_code == 200
