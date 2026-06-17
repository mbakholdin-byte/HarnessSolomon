"""Phase 4.3+ v1.15.0: HTTP long-poll fallback for Elicitation.

Endpoints:
    - ``GET  /api/v1/elicitation/poll?session=S`` — long-poll (max
      ``hooks_elicitation_longpoll_timeout_s`` seconds) waiting for the
      next pending question. Returns the question JSON immediately if
      one is already pending; otherwise polls
      ``broker.pending()`` at ``hooks_elicitation_longpoll_interval_s``
      until one arrives or the timeout elapses. On timeout returns 200
      with an empty body (no pending question right now — caller should
      retry) so callers can distinguish "disabled" (403) from "nothing
      yet" (200 empty).
    - ``POST /api/v1/elicitation/answer`` — submit an answer for a
      pending question. Body:
      ``{"session_id": "...", "question_id": "...", "answer": "..."}``.
      Returns 200 on success, 404 if the question_id is unknown or
      already resolved.

This module is a fallback transport — the primary transport is the
WebSocket at ``/api/v1/elicitation/ws`` (see
:mod:`harness.server.routes.elicitation`). Operators opt in via
``hooks_elicitation_longpoll_enabled`` (default False). When disabled,
both endpoints return 403.

Trust boundary: stdlib + fastapi + pydantic only. NO imports of
``harness.agents`` or other production modules. The broker lives in
:mod:`harness.elicitation` and is imported lazily inside the handlers
so this module can be loaded even when the broker is unconfigured
(e.g. in a stripped-down test app).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field


logger = logging.getLogger("harness.server.routes.elicitation_longpoll")

router = APIRouter()


# === Pydantic models ===


class LongPollAnswer(BaseModel):
    """Body schema for ``POST /api/v1/elicitation/answer``.

    ``session_id`` is optional — the broker keys questions by
    ``question_id`` (process-global UUID hex), so session scoping is
    informational only. It's accepted for API symmetry with the
    long-poll query parameter.
    """

    session_id: str | None = Field(
        default=None,
        description="Optional session scope (informational — broker is process-global).",
    )
    question_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="The pending question_id returned by /poll.",
    )
    answer: str = Field(
        ...,
        min_length=0,
        description="The user's answer. Empty string is allowed (treated as a real answer).",
    )


class LongPollQuestion(BaseModel):
    """Response schema for ``GET /api/v1/elicitation/poll``."""

    question_id: str
    question: str
    options: list[str]
    default_answer: str
    created_at: float


# === Guards ===


def _is_longpoll_enabled(request: Request) -> bool:
    """Read the enable flag from settings.

    Reads ``app.state.hooks_elicitation_longpoll_enabled`` first (set
    by the lifespan in app.py when the feature is wired); falls back
    to constructing ``Settings()`` directly (matches the WS route's
    pattern, which also constructs Settings() inside the handler).
    """
    cached = getattr(
        request.app.state, "hooks_elicitation_longpoll_enabled", None,
    )
    if cached is not None:
        return bool(cached)
    # Lazy fallback — matches elicitation.py WS route style.
    from harness.config import Settings

    return bool(Settings().hooks_elicitation_longpoll_enabled)


def _longpoll_settings(request: Request) -> tuple[float, float]:
    """Return (timeout_s, interval_s) from app.state or Settings()."""
    timeout_s = getattr(
        request.app.state, "hooks_elicitation_longpoll_timeout_s", None,
    )
    interval_s = getattr(
        request.app.state, "hooks_elicitation_longpoll_interval_s", None,
    )
    if timeout_s is None or interval_s is None:
        from harness.config import Settings

        s = Settings()
        timeout_s = float(
            timeout_s if timeout_s is not None
            else s.hooks_elicitation_longpoll_timeout_s
        )
        interval_s = float(
            interval_s if interval_s is not None
            else s.hooks_elicitation_longpoll_interval_s
        )
    return float(timeout_s), float(interval_s)


# === Handlers ===


@router.get("/poll")
async def elicitation_poll(
    request: Request,
    session: str | None = Query(
        default=None,
        description="Optional session id (informational — broker is process-global).",
    ),
) -> LongPollQuestion:
    """Long-poll the broker for the next pending question.

    Behaviour:
        - If long-poll is disabled → 403.
        - If a pending question exists right now → return it (200).
        - Otherwise poll every ``interval_s`` up to ``timeout_s``.
          On timeout → 200 with empty body via raising HTTPException(200)?
          No — FastAPI doesn't allow 200 + empty via raise cleanly. We
          instead return a 404 with ``detail="no_pending_question"`` so
          the caller can distinguish "disabled" (403), "timeout" (404),
          and "got one" (200). (Spec also allows empty body on timeout;
          we chose 404 because FastAPI serialises a clean JSON error
          body which is easier for clients to parse than an empty one.)
    """
    if not _is_longpoll_enabled(request):
        raise HTTPException(status_code=403, detail="longpoll_disabled")

    from harness.elicitation import ElicitationBroker

    broker = ElicitationBroker.get()
    timeout_s, interval_s = _longpoll_settings(request)
    deadline = time.monotonic() + timeout_s

    # Fast path: a question is already pending.
    pending = broker.pending()
    if pending:
        return _question_to_response(pending[0])

    # Slow path: poll until one arrives or we time out.
    while time.monotonic() < deadline:
        await asyncio.sleep(interval_s)
        pending = broker.pending()
        if pending:
            return _question_to_response(pending[0])

    # Timeout — no pending question arrived in the window.
    logger.debug(
        "elicitation longpoll: timeout after %.1fs (session=%r)",
        timeout_s, session,
    )
    raise HTTPException(status_code=404, detail="no_pending_question")


@router.post("/answer")
async def elicitation_answer(
    payload: LongPollAnswer,
    request: Request,
) -> dict[str, Any]:
    """Submit an answer for a pending question.

    Returns:
        ``{"accepted": True, "question_id": "..."}`` on success.

    Status codes:
        - 200: answer accepted, future resolved.
        - 403: long-poll disabled.
        - 404: question_id unknown or already resolved (broker.answer
          returned False).
    """
    if not _is_longpoll_enabled(request):
        raise HTTPException(status_code=403, detail="longpoll_disabled")

    from harness.elicitation import ElicitationBroker

    broker = ElicitationBroker.get()
    ok = broker.answer(payload.question_id, payload.answer)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"unknown_or_resolved_question: {payload.question_id!r}",
        )
    logger.debug(
        "elicitation longpoll: answered qid=%s (session=%r)",
        payload.question_id, payload.session_id,
    )
    return {
        "accepted": True,
        "question_id": payload.question_id,
        "session_id": payload.session_id,
    }


# === Helpers ===


def _question_to_response(pq: Any) -> LongPollQuestion:
    """Convert a ``PendingQuestion`` to the response schema."""
    return LongPollQuestion(
        question_id=pq.question_id,
        question=pq.question,
        options=list(pq.options or []),
        default_answer=pq.default_answer,
        created_at=pq.created_at,
    )


__all__ = ["router", "LongPollAnswer", "LongPollQuestion"]
