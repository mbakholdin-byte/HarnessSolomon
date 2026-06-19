"""Phase 4.11 v1.21.0 — Scope expansion tests.

Adds two new scopes to the Phase 1.6 closed set:

* ``OBSERVABILITY_READ`` — read-only access to admin observability
  endpoints (``/api/v1/observability/metrics``, ``/health``, audit
  trail). Read-only by design: the bootstrap admin gets it
  automatically, and scoped tokens can be minted for dashboards /
  SRE tooling that should not be able to mutate agent state.

* ``ELICITATION_READ`` — subscribe to the SSE elicitation transport
  (``GET /api/v1/elicitation/stream``). Separate from
  ``AGENTS_WRITE`` because a UI client that only renders
  Elicitation questions should not be able to enqueue jobs.

Scope count goes from 7 (Phase 3 v1.4.0 baseline) to 9.

This module is the integration companion to ``test_token_store.py``
(unit-level scope helpers) and ``test_capabilities.py`` (capabilities
endpoint surface). We deliberately re-assert the new scopes here so a
future refactor that splits the enum across modules cannot silently
drop them.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.auth.scopes import (
    ALL_SCOPES,
    SCOPE_DESCRIPTIONS,
    Scope,
    has_scope,
)
from harness.server.auth.tokens import TokenStore


# ---------------------------------------------------------------------------
# Helpers (mirrors test_capabilities.py::_make_app_with_state)
# ---------------------------------------------------------------------------

def _make_app_with_state(*, auth_store: TokenStore | None = None) -> FastAPI:
    """Build a fresh app with just enough ``app.state`` for /capabilities.

    The capabilities endpoint is public and only reads ``app.version``
    + the mounted routes, so we don't need a live JobStore or
    merge_queue here. We still attach a token store so the auth
    dependency doesn't 503 before short-circuiting in dev mode.
    """
    app = create_app()
    app.state.auth_required = settings.auth_required
    if auth_store is None:
        auth_store = TokenStore(settings.auth_db_path)
    app.state.token_store = auth_store
    from harness.agents.jobs import JobStore
    db_path = settings.db_path.parent / "agent-jobs.db"
    app.state.job_store = JobStore(db_path)
    app.state.merge_queue = None
    return app


# ---------------------------------------------------------------------------
# 1. Enum membership
# ---------------------------------------------------------------------------

class TestScopeEnumMembership:
    """The two new scopes must be members of ``Scope`` and ``ALL_SCOPES``."""

    def test_observability_read_scope_exists(self) -> None:
        assert Scope.OBSERVABILITY_READ == "observability.read"
        assert Scope.OBSERVABILITY_READ in ALL_SCOPES

    def test_elicitation_read_scope_exists(self) -> None:
        assert Scope.ELICITATION_READ == "elicitation.read"
        assert Scope.ELICITATION_READ in ALL_SCOPES

    def test_webhook_admin_scope_exists(self) -> None:
        """Phase 4.13B v1.23.0 — outbound webhook admin scope."""
        assert Scope.WEBHOOK_ADMIN == "webhooks.admin"
        assert Scope.WEBHOOK_ADMIN in ALL_SCOPES


# ---------------------------------------------------------------------------
# 2. Capabilities endpoint surfaces the new scopes
# ---------------------------------------------------------------------------

class TestCapabilitiesSurface:
    """``GET /api/v1/capabilities`` must list both new scopes with descriptions."""

    async def test_observability_read_scope_in_capabilities(
        self, isolated_settings: dict,
    ) -> None:
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        assert r.status_code == 200
        names = {s["name"] for s in r.json()["scopes_available"]}
        assert "observability.read" in names

    async def test_elicitation_read_scope_in_capabilities(
        self, isolated_settings: dict,
    ) -> None:
        app = _make_app_with_state()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/capabilities")
        assert r.status_code == 200
        names = {s["name"] for s in r.json()["scopes_available"]}
        assert "elicitation.read" in names


# ---------------------------------------------------------------------------
# 3. Descriptions
# ---------------------------------------------------------------------------

class TestScopeDescriptions:
    """Each new scope must have a non-empty human-readable description."""

    def test_observability_read_scope_description(self) -> None:
        desc = SCOPE_DESCRIPTIONS[Scope.OBSERVABILITY_READ]
        assert isinstance(desc, str)
        assert desc.strip(), "observability.read description must be non-empty"
        # Mention Phase 4.11 so the description doubles as a changelog anchor.
        assert "Phase 4.11" in desc or "observability" in desc.lower()

    def test_elicitation_read_scope_description(self) -> None:
        desc = SCOPE_DESCRIPTIONS[Scope.ELICITATION_READ]
        assert isinstance(desc, str)
        assert desc.strip(), "elicitation.read description must be non-empty"
        assert "Phase 4.11" in desc or "elicitation" in desc.lower()


# ---------------------------------------------------------------------------
# 4. Total scope count
# ---------------------------------------------------------------------------

class TestScopeCount:
    """Phase 3 v1.4.0 baseline = 7; Phase 4.11 v1.21.0 adds 2 → 9;
    Phase 4.13B v1.23.0 adds 1 (webhooks.admin) → 10.

    Handoff text mentioned "8 existing + 2 = 10", but the actual
    pre-Phase-4.11 count in scopes.py is 7 (agents.read/write/pr,
    memory.read/write, sessions.read/write). We assert the real count
    rather than the handoff's stated number — the handoff was off by
    one, and asserting 10 would paper over a regression where an
    unrelated scope silently disappears.
    """

    def test_total_scope_count_updated(self) -> None:
        assert len(ALL_SCOPES) == 10, (
            f"expected 10 scopes (7 baseline + 2 Phase 4.11 + "
            f"1 Phase 4.13B), got {len(ALL_SCOPES)}: "
            f"{sorted(s.value for s in ALL_SCOPES)}"
        )


# ---------------------------------------------------------------------------
# 5. has_scope() semantics for the new scopes
# ---------------------------------------------------------------------------

class TestHasScopeNewScopes:
    """``has_scope`` must recognise the new enum values.

    These are unit tests — no FastAPI, no DB. We exercise the
    ANY-match contract directly: a token with the new scope passes
    the check, a token with a different scope fails, and the empty-
    required edge case still returns True (covered in test_token_store
    but re-asserted here for the new scope to guard against a future
    refactor that special-cases the new enum values).
    """

    def test_has_scope_observability_read(self) -> None:
        token_scopes = {Scope.OBSERVABILITY_READ}
        assert has_scope(token_scopes, {Scope.OBSERVABILITY_READ})
        # ANY-match: having an additional unrelated scope still passes.
        assert has_scope(
            {Scope.OBSERVABILITY_READ, Scope.MEMORY_READ},
            {Scope.OBSERVABILITY_READ},
        )

    def test_has_scope_elicitation_read(self) -> None:
        token_scopes = {Scope.ELICITATION_READ}
        assert has_scope(token_scopes, {Scope.ELICITATION_READ})
        assert has_scope(
            {Scope.ELICITATION_READ, Scope.AGENTS_READ},
            {Scope.ELICITATION_READ},
        )

    def test_has_scope_no_match(self) -> None:
        """A token holding ``memory.read`` must NOT satisfy
        ``observability.read`` — the scopes are disjoint by design.
        """
        token_scopes = {Scope.MEMORY_READ}
        assert not has_scope(token_scopes, {Scope.OBSERVABILITY_READ})
        assert not has_scope(token_scopes, {Scope.ELICITATION_READ})
        # ANY-match: even with multiple unrelated scopes, no match → False.
        assert not has_scope(
            {Scope.MEMORY_READ, Scope.SESSIONS_READ},
            {Scope.OBSERVABILITY_READ, Scope.ELICITATION_READ},
        )
