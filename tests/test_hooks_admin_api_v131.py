"""v1.31.0 — Tests for /api/v1/hooks/* admin API.

Verifies:
  * GET /api/v1/hooks — list all hooks
  * GET /api/v1/hooks/{id} — single hook details
  * POST /api/v1/hooks/{id}/enable — flip on
  * POST /api/v1/hooks/{id}/disable — flip off
  * 404 for unknown hook ids

Run::

    pytest tests/test_hooks_admin_api_v131.py -v
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness.config import settings
from harness.server.app import create_app
from harness.hooks.registry import reset_registry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_hook_registry() -> None:
    """Reset the process-level hook registry before each test.

    Ensures test isolation — each test starts with a fresh registry
    containing only builtin hooks. The ``create_app()`` call in
    ``client`` will call ``get_registry()`` and re-load builtins.
    """
    reset_registry()


@pytest.fixture
def client() -> TestClient:
    """A TestClient with auth_required=False (open dev mode)."""
    # Monkeypatch settings BEFORE creating the app.
    settings.auth_required = False
    settings.hooks_admin_enabled = True
    app = create_app()
    # Ensure the lifespan had a chance to init the hook runner.
    with TestClient(app) as tc:
        yield tc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListHooks:
    """GET /api/v1/hooks — list all hooks."""

    def test_list_returns_200_with_hooks(self, client: TestClient) -> None:
        """GET /api/v1/hooks returns 200 with a list of builtin hooks."""
        r = client.get("/api/v1/hooks")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert "hooks" in body
        assert "total" in body
        assert body["total"] >= 7, (
            f"expected at least 7 builtin hooks, got {body['total']}"
        )
        # Check that hooks have the expected shape.
        for hook in body["hooks"]:
            assert "hook_id" in hook
            assert "event" in hook
            assert "transport" in hook
            assert "enabled" in hook


class TestGetHook:
    """GET /api/v1/hooks/{id} — single hook details."""

    def test_get_existing_hook(self, client: TestClient) -> None:
        """GET on a known builtin hook returns 200 with details."""
        r = client.get("/api/v1/hooks/builtin.log")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert body["hook_id"] == "builtin.log"
        assert body["event"] == "PreToolUse"
        assert body["transport"] == "builtin"

    def test_get_nonexistent_hook_returns_404(self, client: TestClient) -> None:
        """GET on an unknown hook returns 404."""
        r = client.get("/api/v1/hooks/nonexistent.hook")
        assert r.status_code == 404


class TestEnableHook:
    """POST /api/v1/hooks/{id}/enable — flip on."""

    def test_enable_existing_hook(self, client: TestClient) -> None:
        """POST enable on a builtin hook returns 200 with enabled=True."""
        r = client.post("/api/v1/hooks/builtin.log/enable")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert body["hook_id"] == "builtin.log"
        assert body["enabled"] is True

    def test_enable_nonexistent_hook_returns_404(self, client: TestClient) -> None:
        """POST enable on an unknown hook returns 404."""
        r = client.post("/api/v1/hooks/nonexistent.hook/enable")
        assert r.status_code == 404


class TestDisableHook:
    """POST /api/v1/hooks/{id}/disable — flip off."""

    def test_disable_existing_hook(self, client: TestClient) -> None:
        """POST disable on a builtin hook returns 200 with enabled=False."""
        r = client.post("/api/v1/hooks/builtin.log/disable")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert body["hook_id"] == "builtin.log"
        assert body["enabled"] is False

    def test_disable_nonexistent_hook_returns_404(self, client: TestClient) -> None:
        """POST disable on an unknown hook returns 404."""
        r = client.post("/api/v1/hooks/nonexistent.hook/disable")
        assert r.status_code == 404
