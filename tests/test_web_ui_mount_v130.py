"""WI-07 v1.30.0: Tests for FastAPI /ui mount (SPA fallback + static assets).

Verifies that the Web UI mount at ``/ui`` works correctly:
- Serves ``index.html`` when ``web/dist`` exists and ``web_ui_enabled=True``.
- Returns 404 when ``web/dist`` is missing.
- Returns 404 when ``web_ui_enabled=False`` (even if dist exists).
- SPA fallback: ``/ui/some/unknown/path`` → ``index.html`` (200).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_web_dist(project_root: Path, content: str | None = None) -> Path:
    """Create a minimal ``web/dist/`` tree under ``project_root``.

    Returns the ``dist`` directory.
    """
    if content is None:
        content = "<!DOCTYPE html><html><body>Harness UI</body></html>"
    dist = project_root / "web" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(content, encoding="utf-8")
    # Also create a dummy asset so the assets mount has something to serve.
    (assets / "dummy.js").write_text("/* dummy */", encoding="utf-8")
    return dist


def _build_app(project_root: Path, *, ui_enabled: bool = True) -> "FastAPI":
    """Build a fresh FastAPI app pointed at ``project_root``.

    The harness config uses ``PROJECT_ROOT`` (module-level) for
    resolving ``web_dist_path``, so we monkeypatch it to the test
    directory before calling ``create_app()``.
    """
    from harness.server import app as server_app_module
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        server_app_module, "PROJECT_ROOT", project_root,
    )
    monkeypatch.setattr(settings, "web_ui_enabled", ui_enabled)
    # Also point all storage paths at tmp so the lifespan doesn't
    # try to create dirs outside the test sandbox.
    monkeypatch.setattr(settings, "session_dir", project_root / "data" / "sessions")
    monkeypatch.setattr(settings, "db_path", project_root / "data" / "harness.db")
    monkeypatch.setattr(settings, "project_root", project_root)
    monkeypatch.setattr(settings, "auth_db_path", project_root / "data" / "harness-scope.db")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(
        settings, "webhook_secret", "test-secret-32-chars-enough-hmac",
    )
    # Reset DB init flag so lifespan re-inits against the new path.
    from harness.server.db import sqlite as db_sqlite
    db_sqlite._db_initialized = False
    # Reset auth DB init flag.
    from harness.server.auth import db as auth_db
    auth_db._reset_init_flag()
    # Ensure data dirs exist so lifespan doesn't fail on mkdir.
    (project_root / "data" / "sessions").mkdir(parents=True, exist_ok=True)
    (project_root / "data").mkdir(parents=True, exist_ok=True)
    (project_root / "models" / "embeddings").mkdir(parents=True, exist_ok=True)

    from harness.server.app import create_app
    return create_app()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app_with_dist(tmp_path: Path):
    """App with ``web/dist/index.html`` present."""
    _make_web_dist(tmp_path, content="<h1>Test Harness UI</h1>")
    return _build_app(tmp_path, ui_enabled=True)


@pytest.fixture
def app_without_dist(tmp_path: Path):
    """App where ``web/dist/`` does NOT exist."""
    return _build_app(tmp_path, ui_enabled=True)


@pytest.fixture
def app_ui_disabled(tmp_path: Path):
    """App with ``web/dist/`` present but ``web_ui_enabled=False``."""
    _make_web_dist(tmp_path)
    return _build_app(tmp_path, ui_enabled=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWebUiMount:
    """WI-07: /ui mount behaviour."""

    def test_web_ui_mounted_when_dist_exists(
        self, app_with_dist,
    ) -> None:
        """``GET /ui`` → 200 OK when dist exists."""
        from fastapi.testclient import TestClient
        client = TestClient(app_with_dist)
        r = client.get("/ui")
        assert r.status_code == 200, (
            f"expected 200 for /ui, got {r.status_code}: {r.text[:200]}"
        )
        assert "Test Harness UI" in r.text, (
            f"unexpected response body: {r.text[:200]}"
        )

    def test_web_ui_not_mounted_when_dist_missing(
        self, app_without_dist,
    ) -> None:
        """``GET /ui`` → 404 when dist directory does not exist."""
        from fastapi.testclient import TestClient
        client = TestClient(app_without_dist)
        r = client.get("/ui")
        assert r.status_code == 404, (
            f"expected 404 for /ui (no dist), got {r.status_code}"
        )

    def test_web_ui_disabled_via_config(
        self, app_ui_disabled,
    ) -> None:
        """``GET /ui`` → 404 when ``web_ui_enabled=False``."""
        from fastapi.testclient import TestClient
        client = TestClient(app_ui_disabled)
        r = client.get("/ui")
        assert r.status_code == 404, (
            f"expected 404 for /ui (disabled), got {r.status_code}: {r.text[:200]}"
        )

    def test_spa_fallback_serves_index_html(
        self, app_with_dist,
    ) -> None:
        """``GET /ui/some/unknown/path`` → 200 + index.html content."""
        from fastapi.testclient import TestClient
        client = TestClient(app_with_dist)
        r = client.get("/ui/some/unknown/path")
        assert r.status_code == 200, (
            f"expected 200 for SPA fallback, got {r.status_code}: {r.text[:200]}"
        )
        assert "Test Harness UI" in r.text, (
            f"SPA fallback should serve index.html, got: {r.text[:200]}"
        )
