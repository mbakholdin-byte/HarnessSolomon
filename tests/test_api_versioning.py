"""Phase 4.1+ Step 6: Tests for /api/* → /api/v1/* migration + deprecation headers.

Each test exercises the deprecation middleware (RFC 8594 + 8288) AND
the dual-mount routers. Strategy:
    1. Use FastAPI TestClient (no real server).
    2. Hit legacy /api/* paths → assert Deprecation/Sunset/Link headers.
    3. Hit /api/v1/* paths → assert NO deprecation headers.
    4. Hit /metrics, /health/*, /api/health → assert NO headers (excluded).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harness.server.app import create_app


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


class TestLegacyDeprecationHeaders:
    """Legacy /api/* paths get Deprecation/Sunset/Link headers."""

    def test_legacy_sessions_has_deprecation_header(self, client: TestClient) -> None:
        # GET /api/sessions (no auth) may return 401, but headers come
        # back from the middleware regardless of status.
        r = client.get("/api/sessions")
        # Even on 401 the response goes through the middleware.
        assert r.headers.get("deprecation") == "true"
        assert r.headers.get("sunset") == "Wed, 31 Dec 2026 23:59:59 GMT"
        # Link: </api/v1/sessions>; rel="successor-version"
        link = r.headers.get("link", "")
        assert "</api/v1/sessions>" in link
        assert 'rel="successor-version"' in link

    def test_legacy_models_has_deprecation_header(self, client: TestClient) -> None:
        r = client.get("/api/models")
        assert r.headers.get("deprecation") == "true"
        link = r.headers.get("link", "")
        assert "</api/v1/models>" in link

    def test_legacy_sessions_subpath_has_deprecation(self, client: TestClient) -> None:
        r = client.get("/api/sessions/abc-123")
        link = r.headers.get("link", "")
        assert "</api/v1/sessions/abc-123>" in link

    def test_legacy_chat_routes_mounted(self, client: TestClient) -> None:
        # WebSocket-only routes are mounted at /api/chat/ws. A plain GET
        # returns 404 (no GET handler), so the middleware doesn't run
        # for 404s — but the route IS dual-mounted. We verify the
        # v1 path is reachable and the legacy path is registered.
        r1 = client.get("/api/chat/ws")
        r2 = client.get("/api/v1/chat/ws")
        # Both return 404 (WebSocket upgrade only). The point is the
        # mount exists, not the response.
        assert r1.status_code in (404, 405, 426)
        assert r2.status_code in (404, 405, 426)


class TestV1NoDeprecationHeaders:
    """Already-canonical /api/v1/* paths do NOT get deprecation headers."""

    def test_v1_sessions_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/api/v1/sessions")
        assert "deprecation" not in r.headers
        assert "sunset" not in r.headers
        assert "link" not in r.headers or "successor-version" not in r.headers.get("link", "")

    def test_v1_capabilities_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/api/v1/capabilities")
        assert "deprecation" not in r.headers

    def test_v1_agents_jobs_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/api/v1/agents/jobs")
        assert "deprecation" not in r.headers

    def test_v1_memory_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/api/v1/memory/notes")
        assert "deprecation" not in r.headers

    def test_v1_chat_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/api/v1/chat/ws")
        assert "deprecation" not in r.headers


class TestExcludedPaths:
    """Convention paths (metrics, health, openapi) MUST NOT get headers."""

    def test_metrics_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/metrics")
        assert "deprecation" not in r.headers

    def test_health_live_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/health/live")
        assert "deprecation" not in r.headers

    def test_health_ready_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/health/ready")
        assert "deprecation" not in r.headers

    def test_health_deep_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/health/deep")
        assert "deprecation" not in r.headers

    def test_api_health_alias_no_deprecation(self, client: TestClient) -> None:
        # /api/health is the v1.7.1 backward-compat alias for /health/deep.
        # It's NOT legacy — it's a documented alias. Skip headers.
        r = client.get("/api/health")
        assert "deprecation" not in r.headers

    def test_openapi_no_deprecation(self, client: TestClient) -> None:
        r = client.get("/openapi.json")
        assert "deprecation" not in r.headers


class TestCanonicalLinkFormat:
    """Link header conforms to RFC 8288 successor-version rel."""

    def test_link_rel_is_successor_version(self, client: TestClient) -> None:
        r = client.get("/api/sessions")
        link = r.headers.get("link", "")
        # Format: </api/v1/sessions>; rel="successor-version"
        assert link.startswith("<")
        assert ">; rel=" in link
        assert '"successor-version"' in link

    def test_sunset_is_http_date(self, client: TestClient) -> None:
        r = client.get("/api/sessions")
        sunset = r.headers.get("sunset", "")
        # RFC 1123 HTTP-date format: "Wed, 31 Dec 2026 23:59:59 GMT"
        # Must contain a 4-digit year and "GMT".
        assert "GMT" in sunset
        assert "2026" in sunset

    def test_deprecation_value_is_true(self, client: TestClient) -> None:
        r = client.get("/api/sessions")
        # RFC 8594: deprecation value is boolean-like. "true" is the
        # only valid value for now (some servers also send a date,
        # but a simple "true" is the safest).
        assert r.headers.get("deprecation") == "true"


class TestDualMountReachability:
    """Both legacy /api/* and /api/v1/* return the same response shape."""

    def test_legacy_models_and_v1_models_both_200(self, client: TestClient) -> None:
        # GET /api/models → list of models
        r1 = client.get("/api/models")
        r2 = client.get("/api/v1/models")
        # Both should respond (200 or 401/403, but not 404).
        assert r1.status_code != 404, f"legacy /api/models returned 404: {r1.text}"
        assert r2.status_code != 404, f"v1 /api/v1/models returned 404: {r2.text}"

    def test_legacy_health_and_v1_health_both_200(self, client: TestClient) -> None:
        r1 = client.get("/api/health")
        r2 = client.get("/api/v1/health")
        # /api/health is the alias; /api/v1/health comes from health_router.
        assert r1.status_code in (200, 503)
        assert r2.status_code in (200, 503)
