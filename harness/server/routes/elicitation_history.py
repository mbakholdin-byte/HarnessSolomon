"""Phase 4.8 v1.18.0: HTTP endpoint for Elicitation decision history.

Endpoint:
    - ``GET /api/v1/elicitation/history?session=S&limit=N`` — return a
      JSON array of recent :class:`ElicitationDecisionRecord` rows,
      newest first. ``session`` is optional (exact match). ``limit``
      defaults to 100 and is clamped to ``1..10_000``.

The endpoint opens a fresh :class:`ElicitationDecisionStore` against
``settings.db_path.parent / "agent-jobs.db"`` (the same file the broker
writes to). It is closed at the end of the request — short-lived
readers are cheap because SQLite keeps the page cache warm across
connections within a process.

Failure modes:
    - If the store cannot be opened (e.g. the audit file is missing or
      corrupt) the endpoint returns 503 with a JSON ``detail``. The
      broker is unaffected — it keeps writing best-effort.
    - On any unexpected error the endpoint returns 500 with a JSON
      ``detail`` (FastAPI default).

Trust boundary: stdlib + fastapi + pydantic + ``harness.elicitation``
+ ``harness.config`` only. No ``harness.agents`` imports.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


logger = logging.getLogger("harness.server.routes.elicitation_history")

router = APIRouter()


def _decision_to_dict(rec: Any) -> dict[str, Any]:
    """Serialise an :class:`ElicitationDecisionRecord` to JSON dict.

    Kept inline (rather than a Pydantic response model) so the schema
    can evolve without forcing a breaking change on the wire — extra
    fields are simply added to the dict, and missing fields default
    to ``None``.
    """
    return {
        "decision_id": rec.decision_id,
        "session_id": rec.session_id,
        "request_id": rec.request_id,
        "question_id": rec.question_id,
        "question_preview": rec.question_preview,
        "options": list(rec.options or []),
        "default_answer": rec.default_answer,
        "decision": rec.decision,
        "answer": rec.answer,
        "source": rec.source,
        "latency_ms": rec.latency_ms,
        "ts": rec.ts,
    }


@router.get("/history")
async def elicitation_history(
    request: Request,
    session: str | None = Query(
        default=None,
        description="Optional session_id filter (exact match).",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=10_000,
        description="Max rows to return (1..10_000).",
    ),
) -> list[dict[str, Any]]:
    """Return the persisted Elicitation decision history.

    Reads directly from the shared ``agent-jobs.db`` SQLite file — no
    broker interaction. The broker may be writing concurrently; SQLite
    handles the locking, and our reads are short-lived.
    """
    # Resolve the DB path. Prefer app.state (set by app.py at startup);
    # fall back to Settings() so the route is usable in a stripped-down
    # test app.
    db_path = getattr(request.app.state, "elicitation_decision_db_path", None)
    if db_path is None:
        from harness.config import Settings

        db_path = Settings().db_path.parent / "agent-jobs.db"

    # Lazy import so the route module loads even when the store has
    # import-time side effects (it shouldn't, but we follow the same
    # pattern as the long-poll route).
    from harness.elicitation import ElicitationDecisionStore

    try:
        store = ElicitationDecisionStore(db_path)
    except Exception as exc:  # noqa: BLE001 — surface as 503
        logger.warning(
            "elicitation_history: cannot open decision store at %s: %s",
            db_path, exc,
        )
        raise HTTPException(
            status_code=503,
            detail=f"decision_store_unavailable: {exc}",
        ) from exc

    try:
        records = store.query_history(session_id=session, limit=limit)
    except Exception as exc:  # noqa: BLE001 — surface as 503
        logger.warning(
            "elicitation_history: query failed (session=%r limit=%d): %s",
            session, limit, exc,
        )
        raise HTTPException(
            status_code=503,
            detail=f"decision_store_query_failed: {exc}",
        ) from exc
    finally:
        store.close()

    return [_decision_to_dict(r) for r in records]


__all__ = ["router"]
