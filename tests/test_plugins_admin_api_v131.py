"""v1.31.0 — Tests for /api/v1/plugins/* admin API.

Verifies:
  * GET /api/v1/plugins — list loaded plugins
  * GET /api/v1/plugins/{name} — single plugin details
  * POST /api/v1/plugins/{name}/enable — re-enable disabled plugin
  * POST /api/v1/plugins/{name}/disable — disable (unload)
  * 404 for unknown plugin names

Run::

    pytest tests/test_plugins_admin_api_v131.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.config import settings
from harness.server.app import create_app
from harness.plugins import get_registry, reset_registry, PluginInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_plugin_registry_available() -> None:
    """Make sure a plugin_registry singleton exists and is on app.state.

    Called before create_app() so the lifespan handler finds it.
    We pre-populate one fake plugin so list/get endpoints have data.
    """
    registry = get_registry()
    registry.register_plugin(PluginInfo(
        name="test_plugin",
        version="1.0.0",
        source_path="/fake/test_plugin.py",
        hooks=["OnToolUse"],
        tools=["test_tool"],
        scopes=["test.read"],
    ))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_plugin_registry() -> None:
    """Reset the process-level plugin registry before each test."""
    reset_registry()


@pytest.fixture
def client() -> TestClient:
    """A TestClient with auth_required=False (open dev mode).

    Pre-populates the plugin registry with one fake plugin so
    list/get endpoints have data to return. The lifespan handler
    will skip loading from disk (``plugins_enabled=False`` by
    default), but the route uses ``app.state.plugin_registry``
    which we inject below.
    """
    settings.auth_required = False
    settings.plugins_admin_enabled = True
    settings.plugins_enabled = False  # prevent lifespan from loading disk plugins

    # Pre-populate the registry singleton.
    _ensure_plugin_registry_available()

    app = create_app()
    # Override app.state with our pre-populated registry.
    app.state.plugin_registry = get_registry()

    with TestClient(app) as tc:
        yield tc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestListPlugins:
    """GET /api/v1/plugins — list all plugins."""

    def test_list_returns_200_with_plugins(self, client: TestClient) -> None:
        """GET /api/v1/plugins returns 200 with the test plugin."""
        r = client.get("/api/v1/plugins")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert "plugins" in body
        assert "total" in body
        assert body["total"] >= 1, (
            f"expected at least 1 plugin, got {body['total']}"
        )
        names = {p["name"] for p in body["plugins"]}
        assert "test_plugin" in names
        # Verify shape.
        for p in body["plugins"]:
            assert "name" in p
            assert "version" in p
            assert "enabled" in p
            assert "hooks" in p
            assert "tools" in p


class TestGetPlugin:
    """GET /api/v1/plugins/{name} — single plugin details."""

    def test_get_existing_plugin(self, client: TestClient) -> None:
        """GET on test_plugin returns 200 with details."""
        r = client.get("/api/v1/plugins/test_plugin")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert body["name"] == "test_plugin"
        assert body["version"] == "1.0.0"
        assert body["enabled"] is True
        assert "OnToolUse" in body["hooks"]
        assert "test_tool" in body["tools"]

    def test_get_nonexistent_plugin_returns_404(self, client: TestClient) -> None:
        """GET on an unknown plugin returns 404."""
        r = client.get("/api/v1/plugins/nonexistent_plugin")
        assert r.status_code == 404


class TestEnablePlugin:
    """POST /api/v1/plugins/{name}/enable — re-enable a disabled plugin."""

    def test_enable_existing_plugin(self, client: TestClient) -> None:
        """POST enable on test_plugin returns 200 with enabled=True."""
        r = client.post("/api/v1/plugins/test_plugin/enable")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert body["name"] == "test_plugin"
        assert body["enabled"] is True

    def test_enable_nonexistent_plugin_returns_404(self, client: TestClient) -> None:
        """POST enable on an unknown plugin returns 404."""
        r = client.post("/api/v1/plugins/nonexistent_plugin/enable")
        assert r.status_code == 404


class TestDisablePlugin:
    """POST /api/v1/plugins/{name}/disable — disable (unload)."""

    def test_disable_existing_plugin(self, client: TestClient) -> None:
        """POST disable on test_plugin returns 200 with enabled=False."""
        r = client.post("/api/v1/plugins/test_plugin/disable")
        assert r.status_code == 200, f"unexpected status: {r.status_code} {r.text}"
        body = r.json()
        assert body["name"] == "test_plugin"
        assert body["enabled"] is False

    def test_disable_nonexistent_plugin_returns_404(self, client: TestClient) -> None:
        """POST disable on an unknown plugin returns 404."""
        r = client.post("/api/v1/plugins/nonexistent_plugin/disable")
        assert r.status_code == 404
