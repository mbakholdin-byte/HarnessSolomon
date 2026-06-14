"""Public API for the Phase 1.6 scope-gated auth subsystem.

The auth package owns:
  * ``scopes``  — the closed set of OAuth-style scopes the API exposes,
  * ``tokens``  — the persistent SQLite-backed token store (SHA-256 hashed),
  * ``deps``    — FastAPI dependencies (``get_current_token``,
                  ``require_scope``) that the route modules use,
  * ``db``      — the aiosqlite initialisation helper used by the
                  FastAPI lifespan handler.

Nothing in here imports from ``harness/agents/`` or ``harness/server/agent/``
— the trust boundary goes only one way (``server → agents``), and
``auth`` is a leaf module in the dependency graph.
"""
from __future__ import annotations

from harness.server.auth.scopes import (
    ALL_SCOPES,
    Scope,
    has_scope,
    parse_scopes,
    scope_description,
)

__all__ = [
    "ALL_SCOPES",
    "Scope",
    "has_scope",
    "parse_scopes",
    "scope_description",
]
