"""Phase 1.6 — ``GET /api/v1/sessions`` (thin scope-gated wrapper).

The legacy ``/api/sessions`` route (in :mod:`harness.server.routes.sessions`)
is the open /api/* surface from Phase 0. This ``/api/v1/sessions`` route is
the scope-gated mirror — same data, same shape, but it requires the
``sessions.read`` scope. The two routes share the underlying DB layer
(:mod:`harness.server.db.sqlite`); only the auth surface differs.

Out of scope for Phase 1.6:
  - ``POST /api/v1/sessions`` (create) — no ``sessions.write`` scope
    yet; the legacy open endpoint stays the path for now
  - ``GET /api/v1/sessions/{id}`` (single-session lookup) — the
    legacy endpoint covers this and we don't want to duplicate
    logic for no clear win
  - Listing messages within a session — also covered by legacy
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope
from harness.server.db import sqlite as db_sqlite
from harness.server.db.models import Session

router = APIRouter()

# Created at import time and shared across requests.
_sessions_read = require_scope(Scope.SESSIONS_READ)


@router.get("", response_model=list[Session])
async def list_sessions_v1(
    recent: int = 50,
    _token: Any = Depends(_sessions_read),
) -> list[Session]:
    """List the most recently updated sessions (Phase 1.6).

    Phase 1.6: requires ``sessions.read`` scope. The shape is
    the same as the legacy ``/api/sessions`` route so a client
    can swap endpoints by changing only the prefix.
    """
    if recent < 0 or recent > 200:
        raise HTTPException(
            status_code=422, detail="recent must be between 0 and 200",
        )
    if recent == 0:
        return []
    return await db_sqlite.list_sessions(limit=recent)


__all__ = ["router"]
