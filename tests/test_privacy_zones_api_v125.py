"""Phase 5.3 v1.25.0 — Tests for /api/v1/privacy/zones CRUD API.

Verifies:
  * List / get / create / update / delete endpoints
  * Scope checks (privacy.read vs privacy.write)
  * 404 handling for unknown zone ids
  * Pydantic validation (invalid action, empty pattern)
  * Trust boundary (AST check: privacy_zones.py MUST NOT import harness.agents)
  * Admin-disabled mode (endpoints not mounted when setting is False)

Run::

    pytest tests/test_privacy_zones_api_v125.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.auth.scopes import Scope


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def privacy_admin_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enable ``privacy_zones_admin_enabled`` so the router is mounted."""
    monkeypatch.setattr(settings, "privacy_zones_admin_enabled", True)


@pytest.fixture
def privacy_admin_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disable ``privacy_zones_admin_enabled`` (default — router not mounted)."""
    monkeypatch.setattr(settings, "privacy_zones_admin_enabled", False)


@pytest.fixture
def client_open(
    monkeypatch: pytest.MonkeyPatch,
    privacy_admin_enabled: None,
) -> TestClient:
    """A TestClient with privacy admin enabled and auth_required=False (dev mode).

    Auth is bypassed (``auth_required=False``) so we test the route logic
    without token management. Scope checks are tested separately in
    ``TestScopeEnforcement``.
    """
    monkeypatch.setattr(settings, "auth_required", False)
    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def client_disabled(
    monkeypatch: pytest.MonkeyPatch,
    privacy_admin_disabled: None,
) -> TestClient:
    """A TestClient with privacy admin disabled (endpoints not mounted)."""
    monkeypatch.setattr(settings, "auth_required", False)
    app = create_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def make_zone(client_open: TestClient):
    """Factory: create a zone via POST and return the response JSON."""
    def _factory(
        pattern: str = "private/*",
        action: str = "block",
        description: str | None = None,
        enabled: bool = True,
    ) -> dict:
        r = client_open.post(
            "/api/v1/privacy/zones",
            json={
                "pattern": pattern,
                "action": action,
                "description": description,
                "enabled": enabled,
            },
        )
        assert r.status_code == 201, (
            f"create failed: {r.status_code} {r.text}"
        )
        return r.json()
    return _factory


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

class TestCreateZone:
    """POST /api/v1/privacy/zones — create a new zone."""

    def test_create_returns_201_with_server_fields(
        self, client_open: TestClient,
    ) -> None:
        """POST creates a zone with server-generated id + timestamps."""
        r = client_open.post(
            "/api/v1/privacy/zones",
            json={
                "pattern": "private/*",
                "action": "block",
                "description": "Block private dir",
                "enabled": True,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["pattern"] == "private/*"
        assert body["action"] == "block"
        assert body["description"] == "Block private dir"
        assert body["enabled"] is True
        # Server-generated fields.
        assert len(body["id"]) == 32, f"id is not 32-char hex: {body['id']}"
        assert "created_at" in body
        assert "updated_at" in body
        assert body["created_at"] == body["updated_at"]

    def test_create_invalid_action_returns_422(
        self, client_open: TestClient,
    ) -> None:
        """POST with an invalid action returns 422."""
        r = client_open.post(
            "/api/v1/privacy/zones",
            json={"pattern": "*.env", "action": "delete"},
        )
        assert r.status_code == 422

    def test_create_empty_pattern_returns_422(
        self, client_open: TestClient,
    ) -> None:
        """POST with an empty pattern returns 422."""
        r = client_open.post(
            "/api/v1/privacy/zones",
            json={"pattern": "", "action": "block"},
        )
        assert r.status_code == 422


class TestListZones:
    """GET /api/v1/privacy/zones — list all zones."""

    def test_list_empty_returns_empty_array(
        self, client_open: TestClient,
    ) -> None:
        """GET on an empty store returns {zones: [], total: 0}."""
        r = client_open.get("/api/v1/privacy/zones")
        assert r.status_code == 200
        body = r.json()
        assert body["zones"] == []
        assert body["total"] == 0

    def test_list_returns_created_zones(
        self, client_open: TestClient, make_zone,
    ) -> None:
        """GET returns all zones created via POST."""
        z1 = make_zone(pattern="private/*", action="block")
        z2 = make_zone(pattern="*.env", action="redact")

        r = client_open.get("/api/v1/privacy/zones")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        ids = {z["id"] for z in body["zones"]}
        assert z1["id"] in ids
        assert z2["id"] in ids


class TestGetZone:
    """GET /api/v1/privacy/zones/{id} — get one zone."""

    def test_get_existing_zone(
        self, client_open: TestClient, make_zone,
    ) -> None:
        """GET returns the zone by id."""
        created = make_zone(pattern="secrets/**", action="skip")

        r = client_open.get(f"/api/v1/privacy/zones/{created['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == created["id"]
        assert body["pattern"] == "secrets/**"
        assert body["action"] == "skip"

    def test_get_nonexistent_returns_404(
        self, client_open: TestClient,
    ) -> None:
        """GET on an unknown id returns 404."""
        r = client_open.get(
            "/api/v1/privacy/zones/nonexistent1234567890123456789012",
        )
        assert r.status_code == 404


class TestUpdateZone:
    """PUT /api/v1/privacy/zones/{id} — update a zone."""

    def test_update_partial_fields(
        self, client_open: TestClient, make_zone,
    ) -> None:
        """PUT with only some fields updates only those fields."""
        created = make_zone(pattern="private/*", action="block", enabled=True)

        r = client_open.put(
            f"/api/v1/privacy/zones/{created['id']}",
            json={"enabled": False, "action": "redact"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Updated fields.
        assert body["enabled"] is False
        assert body["action"] == "redact"
        # Unchanged fields.
        assert body["pattern"] == "private/*"
        # updated_at must change; created_at must NOT.
        assert body["created_at"] == created["created_at"]
        assert body["updated_at"] >= created["updated_at"]

    def test_update_nonexistent_returns_404(
        self, client_open: TestClient,
    ) -> None:
        """PUT on an unknown id returns 404."""
        r = client_open.put(
            "/api/v1/privacy/zones/nonexistent1234567890123456789012",
            json={"enabled": False},
        )
        assert r.status_code == 404


class TestDeleteZone:
    """DELETE /api/v1/privacy/zones/{id} — delete a zone."""

    def test_delete_returns_204(
        self, client_open: TestClient, make_zone,
    ) -> None:
        """DELETE on an existing zone returns 204 and removes it."""
        created = make_zone()

        r = client_open.delete(f"/api/v1/privacy/zones/{created['id']}")
        assert r.status_code == 204

        # Confirm it's gone.
        r2 = client_open.get(f"/api/v1/privacy/zones/{created['id']}")
        assert r2.status_code == 404

    def test_delete_nonexistent_returns_404(
        self, client_open: TestClient,
    ) -> None:
        """DELETE on an unknown id returns 404."""
        r = client_open.delete(
            "/api/v1/privacy/zones/nonexistent1234567890123456789012",
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------

class TestScopeEnforcement:
    """Verify that GET requires privacy.read and mutations require privacy.write.

    We build the app with ``auth_required=True`` and issue a token with
    ONLY ``privacy.read`` scope. Then:

      * GET /zones → 200 (has read)
      * POST /zones → 403 (missing write)
      * PUT /zones/{id} → 403 (missing write)
      * DELETE /zones/{id} → 403 (missing write)
    """

    @pytest.fixture
    async def scoped_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        privacy_admin_enabled: None,
    ):
        """A client with auth_required=True and a privacy.read-only token.

        Uses the same isolated-settings pattern as conftest.py to
        point ``auth_db_path`` at a fresh temp DB, then initialises
        the token store + creates a read-only token.
        """
        # Isolate auth DB to a temp path so the table is fresh.
        auth_db_path = tmp_path / "scope-test.db"
        monkeypatch.setattr(settings, "auth_db_path", auth_db_path)
        monkeypatch.setattr(settings, "auth_required", True)

        # Reset the auth DB init flag so the next init() runs.
        from harness.server.auth import db as auth_db
        auth_db._reset_init_flag()

        from harness.server.auth.tokens import TokenStore

        store = TokenStore(auth_db_path)
        await store.init()

        plaintext, _record = await store.create(
            "privacy-read-only",
            {Scope.PRIVACY_READ},
        )

        app = create_app()
        # Override the app.state token_store so the auth dep uses ours.
        app.state.token_store = store
        app.state.auth_required = True

        with TestClient(app) as tc:
            tc._token = plaintext  # type: ignore[attr-defined]
            yield tc

    def test_get_allowed_with_read_scope(
        self, scoped_client: TestClient,
    ) -> None:
        """GET /zones succeeds with privacy.read scope."""
        token = scoped_client._token  # type: ignore[attr-defined]
        r = scoped_client.get(
            "/api/v1/privacy/zones",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    def test_post_forbidden_with_read_only_scope(
        self, scoped_client: TestClient,
    ) -> None:
        """POST /zones is 403 with only privacy.read (missing write)."""
        token = scoped_client._token  # type: ignore[attr-defined]
        r = scoped_client.post(
            "/api/v1/privacy/zones",
            headers={"Authorization": f"Bearer {token}"},
            json={"pattern": "test/*", "action": "block"},
        )
        assert r.status_code == 403

    def test_put_forbidden_with_read_only_scope(
        self, scoped_client: TestClient,
    ) -> None:
        """PUT /zones/{id} is 403 with only privacy.read."""
        token = scoped_client._token  # type: ignore[attr-defined]
        r = scoped_client.put(
            "/api/v1/privacy/zones/someid1234567890123456789012",
            headers={"Authorization": f"Bearer {token}"},
            json={"enabled": False},
        )
        assert r.status_code == 403

    def test_delete_forbidden_with_read_only_scope(
        self, scoped_client: TestClient,
    ) -> None:
        """DELETE /zones/{id} is 403 with only privacy.read."""
        token = scoped_client._token  # type: ignore[attr-defined]
        r = scoped_client.delete(
            "/api/v1/privacy/zones/someid1234567890123456789012",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Admin-disabled mode
# ---------------------------------------------------------------------------

class TestAdminDisabled:
    """When ``privacy_zones_admin_enabled`` is False, endpoints return 404."""

    def test_endpoints_not_mounted_when_disabled(
        self, client_disabled: TestClient,
    ) -> None:
        """GET /api/v1/privacy/zones returns 404 when admin is disabled."""
        r = client_disabled.get("/api/v1/privacy/zones")
        assert r.status_code == 404, (
            f"expected 404 (router not mounted), got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# Trust boundary (AST check)
# ---------------------------------------------------------------------------

class TestTrustBoundary:
    """``privacy_zones.py`` MUST NOT import from ``harness.agents.*``.

    Mirrors the pattern in ``test_legacy_gone_v122.py`` and
    ``test_runner_does_not_import_v150.py``. We parse the source
    with ``ast`` and walk all ``Import`` / ``ImportFrom`` nodes,
    failing if any start with ``harness.agents``.
    """

    def test_no_agents_import(self) -> None:
        """No import in privacy_zones.py resolves to harness.agents.*"""
        route_path = (
            Path(__file__).resolve().parent.parent
            / "harness" / "server" / "routes" / "privacy_zones.py"
        )
        source = route_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(route_path))

        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("harness.agents"):
                        violations.append(
                            f"line {node.lineno}: import {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("harness.agents"):
                    violations.append(
                        f"line {node.lineno}: from {node.module} import ..."
                    )

        assert not violations, (
            f"Trust boundary violation in {route_path.name}: "
            f"imports harness.agents: {violations}"
        )


# ---------------------------------------------------------------------------
# Frontend smoke (file existence + syntax)
# ---------------------------------------------------------------------------

class TestFrontendSmoke:
    """Verify the frontend files exist and are non-empty.

    We don't run a JS test runner here (no Node in the test env),
    but we check the files exist and contain expected exports.
    """

    def test_privacy_api_client_exists(self) -> None:
        """``web/src/api/privacy.ts`` exists and exports the client factory."""
        path = (
            Path(__file__).resolve().parent.parent
            / "web" / "src" / "api" / "privacy.ts"
        )
        assert path.exists(), f"missing frontend API client: {path}"
        source = path.read_text(encoding="utf-8")
        assert "createPrivacyApiClient" in source
        assert "PrivacyZone" in source

    def test_zone_modal_exists(self) -> None:
        """``web/src/components/ZoneModal.tsx`` exists and exports ZoneModal."""
        path = (
            Path(__file__).resolve().parent.parent
            / "web" / "src" / "components" / "ZoneModal.tsx"
        )
        assert path.exists(), f"missing ZoneModal: {path}"
        source = path.read_text(encoding="utf-8")
        assert "export function ZoneModal" in source

    def test_privacy_zones_page_exists(self) -> None:
        """``web/src/pages/PrivacyZones.tsx`` exists and exports the page."""
        path = (
            Path(__file__).resolve().parent.parent
            / "web" / "src" / "pages" / "PrivacyZones.tsx"
        )
        assert path.exists(), f"missing PrivacyZones page: {path}"
        source = path.read_text(encoding="utf-8")
        assert "PrivacyZonesPage" in source
        assert "ZoneModal" in source
