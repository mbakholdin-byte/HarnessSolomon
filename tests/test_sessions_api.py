"""Tests for Sessions REST API (Шаг 3).

Per TDD (RED → GREEN → REFACTOR): tests first, then minimal implementation.

Endpoints under test:
  GET    /api/sessions                 — list sessions
  POST   /api/sessions                 — create session
  GET    /api/sessions/{id}            — get session
  DELETE /api/sessions/{id}            — delete session
  GET    /api/sessions/{id}/messages   — list messages
  POST   /api/sessions/{id}/messages   — add message
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.db import sqlite as db_sqlite


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncClient:
    """Test client with isolated data dir."""
    data_dir = tmp_path / "harness-data"
    monkeypatch.setattr(settings, "session_dir", data_dir / "sessions")
    monkeypatch.setattr(settings, "db_path", data_dir / "harness.db")
    db_sqlite._db_initialized = False

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# === GET /api/sessions — list ===

async def test_list_sessions_empty(client: AsyncClient) -> None:
    """Empty database → empty list."""
    r = await client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json() == []


async def test_list_sessions_returns_created(client: AsyncClient) -> None:
    """Created sessions appear in list."""
    s1 = await client.post(
        "/api/sessions", json={"title": "first", "model": "MiniMax-M2.7"}
    )
    s2 = await client.post(
        "/api/sessions", json={"title": "second", "model": "glm-4.7"}
    )
    assert s1.status_code == 201
    assert s2.status_code == 201

    r = await client.get("/api/sessions")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    titles = {s["title"] for s in data}
    assert titles == {"first", "second"}


# === POST /api/sessions — create ===

async def test_create_session_minimal(client: AsyncClient) -> None:
    """Create with just title + model."""
    r = await client.post(
        "/api/sessions", json={"title": "test", "model": "MiniMax-M2.7"}
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "test"
    assert data["model"] == "MiniMax-M2.7"
    assert "id" in data
    assert "created_at" in data
    assert "updated_at" in data
    assert data["message_count"] == 0
    assert data["total_tokens"] == 0
    assert data["total_cost"] == 0.0


async def test_create_session_missing_fields(client: AsyncClient) -> None:
    """Missing title or model → 422."""
    r = await client.post("/api/sessions", json={"title": "x"})
    assert r.status_code == 422
    r = await client.post("/api/sessions", json={"model": "x"})
    assert r.status_code == 422


# === GET /api/sessions/{id} ===

async def test_get_session(client: AsyncClient) -> None:
    """Get by id returns session."""
    created = (await client.post(
        "/api/sessions", json={"title": "x", "model": "MiniMax-M2.7"}
    )).json()
    sid = created["id"]

    r = await client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["id"] == sid


async def test_get_session_not_found(client: AsyncClient) -> None:
    """Unknown id → 404."""
    r = await client.get("/api/sessions/nonexistent-id")
    assert r.status_code == 404


# === DELETE /api/sessions/{id} ===

async def test_delete_session(client: AsyncClient) -> None:
    """Delete by id → 204, then GET returns 404."""
    created = (await client.post(
        "/api/sessions", json={"title": "x", "model": "MiniMax-M2.7"}
    )).json()
    sid = created["id"]

    r = await client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 204

    r = await client.get(f"/api/sessions/{sid}")
    assert r.status_code == 404


async def test_delete_session_not_found(client: AsyncClient) -> None:
    """Delete unknown id → 404."""
    r = await client.delete("/api/sessions/nonexistent-id")
    assert r.status_code == 404


# === Messages ===

async def test_add_and_list_messages(client: AsyncClient) -> None:
    """Add user message, list returns it."""
    created = (await client.post(
        "/api/sessions", json={"title": "x", "model": "MiniMax-M2.7"}
    )).json()
    sid = created["id"]

    # Empty messages first
    r = await client.get(f"/api/sessions/{sid}/messages")
    assert r.status_code == 200
    assert r.json() == []

    # Add message
    r = await client.post(
        f"/api/sessions/{sid}/messages",
        json={"role": "user", "content": "Привет"},
    )
    assert r.status_code == 201
    msg = r.json()
    assert msg["role"] == "user"
    assert msg["content"] == "Привет"
    assert "id" in msg
    assert "ts" in msg

    # List now has 1 message
    r = await client.get(f"/api/sessions/{sid}/messages")
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Привет"


async def test_add_message_to_unknown_session(client: AsyncClient) -> None:
    """Add message to non-existent session → 404."""
    r = await client.post(
        "/api/sessions/nonexistent/messages",
        json={"role": "user", "content": "x"},
    )
    assert r.status_code == 404


async def test_add_message_invalid_role(client: AsyncClient) -> None:
    """Invalid role → 422."""
    created = (await client.post(
        "/api/sessions", json={"title": "x", "model": "MiniMax-M2.7"}
    )).json()
    sid = created["id"]
    r = await client.post(
        f"/api/sessions/{sid}/messages",
        json={"role": "admin", "content": "x"},
    )
    assert r.status_code == 422


async def test_message_count_increments(client: AsyncClient) -> None:
    """Adding 2 messages → session.message_count == 2."""
    created = (await client.post(
        "/api/sessions", json={"title": "x", "model": "MiniMax-M2.7"}
    )).json()
    sid = created["id"]

    for i in range(2):
        await client.post(
            f"/api/sessions/{sid}/messages",
            json={"role": "user", "content": f"msg {i}"},
        )

    s = (await client.get(f"/api/sessions/{sid}")).json()
    assert s["message_count"] == 2
