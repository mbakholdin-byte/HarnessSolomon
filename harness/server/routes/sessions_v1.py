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

Phase 3 v1.4.0 added:
  - ``POST /api/v1/sessions/{id}/compact`` — requires ``sessions.write``.
    This is a session-control operation (it compacts the *running*
    session's context), not a job-write or a memory-write, so it
    got its own scope. The route delegates to
    :class:`harness.server.agent.compact_trigger.CompactTrigger`.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope
from harness.server.db import sqlite as db_sqlite
from harness.server.db.models import Session

logger = logging.getLogger(__name__)

router = APIRouter()

# Created at import time and shared across requests.
_sessions_read = require_scope(Scope.SESSIONS_READ)
# Phase 3 v1.4.0: ``sessions.write`` is the dedicated scope for
# /compact — semantically NOT a job-write and NOT a memory-write.
_sessions_write = require_scope(Scope.SESSIONS_WRITE)


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


# Phase 3 v1.4.0: response model for /compact (mirrors CompactResult fields).
class _CompactResponse(dict):
    """Duck-typed response — we just return a plain dict for OpenAPI clarity."""


@router.post("/{session_id}/compact")
async def compact_session_v1(
    session_id: str,
    request: Request,
    bypass_cache: bool = False,
    _token: Any = Depends(_sessions_write),
) -> dict[str, Any]:
    """Force-compact a session's context (Phase 3 v1.4.0).

    Requires the ``sessions.write`` scope (separate from
    ``agents.write`` and ``memory.write`` because /compact is a
    session-control operation).

    Returns a JSON body with the same shape as
    :class:`harness.context.compaction.CompactResult`:
    ``{"original_tokens": int, "compacted_tokens": int,
       "summary_preview": str, "cache_hit": bool,
       "saved_tokens": int}``.

    Returns 503 if the compactor / trigger is not wired into the
    app (e.g. dev mode without a real compactor). Returns 200 with
    the result on success.
    """
    # The compactor is wired in lifespan (Phase 3 v1.4.0 Step 5 will
    # create the trigger closure; for now we accept either a bare
    # compactor or a trigger on app.state).
    trigger = getattr(request.app.state, "compact_trigger", None)
    compactor = getattr(request.app.state, "compactor", None)
    if trigger is None and compactor is not None:
        # Build a trigger on demand from the compactor.
        from harness.config import settings as _settings
        from harness.server.agent.compact_trigger import CompactTrigger
        trigger = CompactTrigger(compactor, _settings)
    if trigger is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "compact trigger not wired — set up a compactor in "
                "lifespan or run with --dev mode"
            ),
        )

    # We need the session's messages to compact. The simplest path
    # is to load them from the message store; for now we expect
    # either an explicit messages list on app.state (test path) or
    # a callable that returns them. The full wiring happens in
    # Step 5; for Step 3 we accept an empty list (forces a no-op
    # compact that still returns a valid CompactResult).
    messages: list[dict[str, Any]] = []
    loader = getattr(request.app.state, "load_session_messages", None)
    if callable(loader):
        try:
            loaded = await loader(session_id)  # type: ignore[func-returns-value]
            if isinstance(loaded, list):
                messages = loaded
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "compact_session_v1: load_session_messages failed: %s", exc,
            )

    # The model id for the compact call. We default to the agent's
    # primary model via settings; full wiring will read from the
    # session record.
    from harness.config import settings as _settings
    model = getattr(_settings, "subagent_t1_model", "qwen3:8b") or "qwen3:8b"

    result = await trigger.compact_now(
        messages,
        model,
        session_id=session_id,
        bypass_cache=bypass_cache,
    )
    if result is None:
        # Trigger already audited the failure; return a clean 503 so
        # the client knows the operation did not succeed.
        raise HTTPException(
            status_code=503,
            detail="compact failed (see audit log for details)",
        )
    return {
        "original_tokens": result.original_tokens,
        "compacted_tokens": result.compacted_tokens,
        "summary_preview": result.summary_preview,
        "cache_hit": result.cache_hit,
        "saved_tokens": result.saved_tokens,
    }


__all__ = ["router"]
