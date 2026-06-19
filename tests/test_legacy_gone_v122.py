"""Phase 4.12 v1.22.0: Tests for Legacy /api/* → 410 Gone middleware.

Verifies that when ``settings.legacy_apis_gone_enabled`` is True, every
request to a legacy ``/api/*`` path (NOT ``/api/v1/*``) returns
``HTTP 410 Gone`` with the expected RFC 8594 deprecation/sunset headers,
RFC 8288 successor-version Link, and a JSON body pointing at the
migration guide. When the flag is False, legacy endpoints continue to
serve (with the existing ``LegacyApiDeprecationMiddleware`` headers).

Also includes an AST-style trust-boundary check: ``legacy_gone.py`` MUST
NOT import from ``harness.agents`` (mirrors the pattern in
``test_runner_does_not_import_v150.py``).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.server.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client_gone_enabled() -> TestClient:
    """A TestClient with ``legacy_apis_gone_enabled=True``.

    Builds the real FastAPI app (so all middleware + routers are wired),
    then flips the master switch on ``app.state`` so the
    :class:`LegacyApisGoneMiddleware` short-circuits legacy paths.
    """
    app = create_app()
    app.state.legacy_apis_gone_enabled = True
    return TestClient(app)


@pytest.fixture
def client_gone_disabled() -> TestClient:
    """A TestClient with ``legacy_apis_gone_enabled=False`` (default).

    Legacy endpoints should continue to serve (status 200 / 401 / etc.)
    — the only legacy-surface change is the deprecation headers added
    by the pre-existing ``LegacyApiDeprecationMiddleware``.
    """
    app = create_app()
    app.state.legacy_apis_gone_enabled = False
    return TestClient(app)


# ---------------------------------------------------------------------------
# Core behaviour: 410 Gone on legacy /api/*
# ---------------------------------------------------------------------------

class TestLegacyGone410:
    """Legacy /api/* paths return 410 Gone when the flag is on."""

    def test_legacy_endpoint_returns_410_gone(
        self, client_gone_enabled: TestClient,
    ) -> None:
        """``GET /api/sessions/S1`` → 410 Gone."""
        r = client_gone_enabled.get("/api/sessions/S1")
        assert r.status_code == 410, (
            f"expected 410 Gone for legacy /api/sessions/S1, "
            f"got {r.status_code}: {r.text}"
        )

    def test_legacy_chat_returns_410(
        self, client_gone_enabled: TestClient,
    ) -> None:
        """``GET /api/chat/ws`` → 410 (legacy chat WebSocket path)."""
        r = client_gone_enabled.get("/api/chat/ws")
        assert r.status_code == 410, (
            f"expected 410 for legacy /api/chat/ws, got {r.status_code}"
        )

    def test_legacy_models_returns_410(
        self, client_gone_enabled: TestClient,
    ) -> None:
        """``GET /api/models`` → 410."""
        r = client_gone_enabled.get("/api/models")
        assert r.status_code == 410, (
            f"expected 410 for legacy /api/models, got {r.status_code}"
        )

    def test_legacy_health_returns_410(
        self, client_gone_enabled: TestClient,
    ) -> None:
        """``GET /api/health`` → 410 (legacy alias, not /api/v1/health)."""
        r = client_gone_enabled.get("/api/health")
        # /api/health is legacy (the /api/v1/health path is canonical).
        # The middleware does NOT special-case it — every /api/* path
        # that isn't /api/v1/* gets 410.
        assert r.status_code == 410, (
            f"expected 410 for legacy /api/health, got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# Opt-in behaviour: flag=False → legacy paths still serve
# ---------------------------------------------------------------------------

class TestLegacyGoneDisabled:
    """When ``legacy_apis_gone_enabled=False``, legacy paths still serve."""

    def test_legacy_endpoint_disabled_setting(
        self, client_gone_disabled: TestClient,
    ) -> None:
        """``legacy_apis_gone_enabled=False`` → legacy endpoint returns
        a normal status (200/401/404/etc.), NOT 410.

        The exact status depends on auth + DB state; we only assert
        it's not the short-circuit 410.
        """
        r = client_gone_disabled.get("/api/sessions/S1")
        assert r.status_code != 410, (
            f"legacy endpoint returned 410 even though "
            f"legacy_apis_gone_enabled=False: status={r.status_code}"
        )


# ---------------------------------------------------------------------------
# Header correctness (RFC 8594 + RFC 8288)
# ---------------------------------------------------------------------------

class TestLegacyGoneHeaders:
    """410 responses carry the required deprecation headers."""

    def test_legacy_endpoint_includes_sunset_header(
        self, client_gone_enabled: TestClient,
    ) -> None:
        r = client_gone_enabled.get("/api/sessions/S1")
        assert r.status_code == 410
        sunset = r.headers.get("sunset", "")
        # RFC 1123 HTTP-date. Mirrors the SUNSET_HTTP_DATE constant.
        assert sunset == "Wed, 31 Dec 2026 23:59:59 GMT", (
            f"Sunset header mismatch: got {sunset!r}"
        )

    def test_legacy_endpoint_includes_deprecation_header(
        self, client_gone_enabled: TestClient,
    ) -> None:
        r = client_gone_enabled.get("/api/sessions/S1")
        assert r.status_code == 410
        # RFC 8594: value is "true" or an HTTP-date. We use "true".
        assert r.headers.get("deprecation") == "true", (
            f"Deprecation header mismatch: "
            f"got {r.headers.get('deprecation')!r}"
        )

    def test_legacy_endpoint_includes_link_header_successor(
        self, client_gone_enabled: TestClient,
    ) -> None:
        r = client_gone_enabled.get("/api/sessions/S1")
        assert r.status_code == 410
        link = r.headers.get("link", "")
        # RFC 8288: rel="successor-version" (RFC 8594 § 3).
        assert "</api/v1/>" in link, (
            f"Link header missing canonical target: {link!r}"
        )
        assert 'rel="successor-version"' in link, (
            f"Link header missing successor-version rel: {link!r}"
        )


# ---------------------------------------------------------------------------
# Non-regression: /api/v1/* is never affected
# ---------------------------------------------------------------------------

class TestV1NotAffected:
    """``/api/v1/*`` paths must never get 410 from the legacy middleware."""

    def test_v1_endpoint_not_affected(
        self, client_gone_enabled: TestClient,
    ) -> None:
        """``GET /api/v1/sessions/S1`` → NOT 410.

        The canonical versioned path must continue to serve
        (200/401/404/etc.) regardless of the legacy_gone flag.
        """
        r = client_gone_enabled.get("/api/v1/sessions/S1")
        assert r.status_code != 410, (
            f"/api/v1/sessions/S1 returned 410 even though it's the "
            f"canonical versioned path: status={r.status_code}"
        )


# ---------------------------------------------------------------------------
# Response body correctness
# ---------------------------------------------------------------------------

class TestLegacyGoneBody:
    """410 response JSON body includes the migration URL."""

    def test_legacy_gone_response_body_includes_migration_url(
        self, client_gone_enabled: TestClient,
    ) -> None:
        r = client_gone_enabled.get("/api/sessions/S1")
        assert r.status_code == 410
        body = r.json()
        assert "migration_url" in body, (
            f"response body missing migration_url: {body!r}"
        )
        assert body["migration_url"] == "https://docs.harness/api/v1-migration", (
            f"migration_url mismatch: got {body['migration_url']!r}"
        )
        # Sanity: the other documented fields are present.
        assert body.get("error") == "Gone"
        assert "message" in body and body["message"]


# ---------------------------------------------------------------------------
# Trust boundary: legacy_gone.py MUST NOT import harness.agents
# ---------------------------------------------------------------------------

class TestLegacyGoneTrustBoundary:
    """AST-style static check: no ``harness.agents`` imports in legacy_gone.

    Mirrors the pattern in ``test_runner_does_not_import_v150.py``.
    The middleware is a leaf Starlette component — it must not pull in
    the agent runtime (which would re-introduce the trust-boundary
    violations that Phase 3 v1.5.0 + Phase 4.x worked to eliminate).
    """

    def test_legacy_gone_does_not_import_harness_agents(self) -> None:
        """``legacy_gone.py`` source contains no ``harness.agents`` import."""
        src_path = Path("harness/server/middleware/legacy_gone.py")
        assert src_path.exists(), (
            f"missing middleware source: {src_path}"
        )
        src = src_path.read_text(encoding="utf-8")
        forbidden_substrings = (
            "from harness.agents",
            "import harness.agents",
            "from harness.agent",   # singular module too
            "import harness.agent",
        )
        for line in src.splitlines():
            stripped = line.strip()
            # Skip blank / comment / docstring lines.
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            for pattern in forbidden_substrings:
                assert pattern not in line, (
                    f"trust-boundary violation: legacy_gone.py imports "
                    f"harness.agents: {line!r}"
                )

    def test_legacy_gone_imports_only_stdlib_and_fastapi(self) -> None:
        """All top-level imports in legacy_gone.py are stdlib or fastapi/starlette.

        Defensive: catches accidental additions of heavy deps (e.g.
        ``harness.config``, ``harness.observability``) that would widen
        the import graph at middleware load time.
        """
        src_path = Path("harness/server/middleware/legacy_gone.py")
        src = src_path.read_text(encoding="utf-8")
        allowed_prefixes = (
            "from __future__",
            "import logging",
            "from typing",
            "from fastapi",
            "from starlette",
            "from harness.server.middleware",  # self-import OK in __init__
        )
        for line in src.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            # __future__ + stdlib + fastapi/starlette are always OK.
            if stripped.startswith(allowed_prefixes):
                continue
            # ``harness.*`` imports are forbidden (except self-references).
            if stripped.startswith(("from harness", "import harness")):
                # Only self-imports into this package are allowed.
                if "harness.server.middleware" in stripped:
                    continue
                pytest.fail(
                    f"trust-boundary: legacy_gone.py has a non-stdlib "
                    f"non-fastapi import: {line!r}"
                )
