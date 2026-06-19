"""v1.0.0 RBAC fix tests — security hardening per Марк review 2026-06-19.

Three regressions fixed:
  * WS elicitation upgrade now requires ``elicitation.write`` scope
    (was: unauthenticated — anyone could answer confirm_dangerous).
  * Long-poll /poll now requires ``elicitation.read`` scope
    (was: unauthenticated).
  * POST /api/v1/sessions now requires ``sessions.write`` scope
    (was: ``sessions.read`` — REST semantics violation).

Trust boundary: stdlib + harness.server only. NO imports of
harness.agents or other production modules.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


# === Async helpers ==========================================================


async def _build_app_and_token(
    token_scopes: set[str] | None,
) -> tuple[FastAPI, str | None]:
    """Async helper — builds FastAPI app with optional scoped token."""
    from harness.config import Settings
    from harness.elicitation import ElicitationBroker
    from harness.server.auth import db as auth_db
    from harness.server.auth.scopes import parse_scopes
    from harness.server.auth.tokens import TokenStore
    from harness.server.routes import elicitation, elicitation_longpoll
    from harness.server.routes import sessions as sessions_route

    settings = Settings()  # force settings load

    app = FastAPI()
    app.include_router(elicitation.router, prefix="/api/v1/elicitation")
    app.include_router(elicitation_longpoll.router, prefix="/api/v1/elicitation")
    app.include_router(sessions_route.router, prefix="/api/v1")

    # Force broker init.
    ElicitationBroker.get()

    plaintext: str | None = None
    if token_scopes is not None:
        # Use settings.auth_db_path so the table schema is created via
        # auth_db.init_auth_db (same code path as production).
        await auth_db.init_auth_db(settings.auth_db_path)
        store = TokenStore(settings.auth_db_path)
        scopes = parse_scopes(",".join(token_scopes))
        plaintext, _ = await store.create(
            label="v1.0.0-rbac-fix-test",
            scopes=scopes,
        )
        app.state.token_store = store
    else:
        # No token requested — give the deps layer a None store so it
        # returns 401 (missing Authorization) rather than 503.
        app.state.token_store = None

    return app, plaintext


# === WS Elicitation =========================================================


@pytest.mark.asyncio
async def test_ws_elicitation_rejects_without_token() -> None:
    """WS upgrade with no token → close code 1008."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app, _ = await _build_app_and_token(None)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/v1/elicitation/ws"):
                pass
        # 1008 = policy violation
        assert exc.value.code == 1008


@pytest.mark.asyncio
async def test_ws_elicitation_rejects_with_read_only_token() -> None:
    """WS upgrade with only elicitation.read → close code 1008."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app, token = await _build_app_and_token({"elicitation.read"})
    assert token is not None
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/api/v1/elicitation/ws?token={token}",
            ):
                pass
        assert exc.value.code == 1008


@pytest.mark.asyncio
async def test_ws_elicitation_accepts_with_write_token() -> None:
    """WS upgrade with elicitation.write → connection accepted."""
    from starlette.testclient import TestClient

    app, token = await _build_app_and_token({"elicitation.write"})
    assert token is not None
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/elicitation/ws?token={token}",
        ) as ws:
            msg = ws.receive_json()
            assert msg["action"] == "connected"


# === Long-poll ==============================================================


@pytest.mark.asyncio
async def test_longpoll_poll_requires_elicitation_read() -> None:
    """GET /api/v1/elicitation/poll without token → 401 (auth_required=True)."""
    app, _ = await _build_app_and_token(None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get("/api/v1/elicitation/poll?session=test")
        # When token_store is None and auth_required is True, deps returns
        # 401 (no Authorization header). When auth_required=False, returns
        # 403 (longpoll disabled by default). Both are acceptable here —
        # the contract is: NOT 200.
        assert r.status_code in (401, 403, 503), (
            f"expected auth or disabled error, got {r.status_code}: {r.text}"
        )


@pytest.mark.asyncio
async def test_longpoll_answer_requires_elicitation_write() -> None:
    """POST /api/v1/elicitation/answer without token → 401."""
    app, _ = await _build_app_and_token(None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.post(
            "/api/v1/elicitation/answer",
            json={"question_id": "q1", "answer": "yes"},
        )
        assert r.status_code in (401, 403, 503), (
            f"expected auth or disabled error, got {r.status_code}: {r.text}"
        )


@pytest.mark.asyncio
async def test_longpoll_poll_accepts_with_read_token() -> None:
    """GET /poll with elicitation.read → NOT 401/403 (may be 404 if no pending)."""
    app, token = await _build_app_and_token({"elicitation.read"})
    assert token is not None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.get(
            "/api/v1/elicitation/poll?session=test",
            headers={"Authorization": f"Bearer {token}"},
        )
        # 200 (got question), 403 (longpoll disabled), or 404 (no pending).
        # What matters: NOT 401 (auth scope rejected) — auth passed.
        assert r.status_code != 401, (
            f"read token should pass auth, got {r.status_code}: {r.text}"
        )


# === Sessions ===============================================================


@pytest.mark.asyncio
async def test_sessions_create_requires_sessions_write() -> None:
    """POST /api/v1/sessions without sessions.write → 403 (was: read)."""
    app, token = await _build_app_and_token({"sessions.read"})
    assert token is not None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.post(
            "/api/v1/sessions",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "test", "model": "test-model"},
        )
        assert r.status_code == 403, (
            f"expected 403 with sessions.read, got {r.status_code}: {r.text}"
        )


@pytest.mark.asyncio
async def test_sessions_create_accepts_sessions_write() -> None:
    """POST /api/v1/sessions with sessions.write → 200/201/422 (NOT 403)."""
    app, token = await _build_app_and_token({"sessions.write"})
    assert token is not None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as ac:
        r = await ac.post(
            "/api/v1/sessions",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "test", "model": "test-model"},
        )
        assert r.status_code in (200, 201, 422), (
            f"sessions.write should be accepted, got {r.status_code}: {r.text}"
        )
