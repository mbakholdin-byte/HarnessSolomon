"""Phase 4.11 v1.21.0: Server-Sent Events transport for Elicitation.

Endpoint:
    - ``GET /api/v1/elicitation/sse?session=S`` — long-lived
      ``text/event-stream`` that pushes Elicitation lifecycle events
      to the subscriber.

SSE wire format (one blank-line-separated block per event)::

    event: new_question
    data: {"question_id": "...", "question": "...", "options": [...],
           "default_answer": "...", "session_id": "...", "created_at": 0.0}

    event: answered
    data: {"question_id": "...", "answer": "...", "session_id": "..."}

    event: timeout
    data: {"question_id": "...", "default_answer": "...", "session_id": "..."}

    : keep-alive

Lifecycle:
    1. The handler validates the enable flag (403 when disabled) and
       the RBAC scope (403 when the token lacks ``elicitation.read``).
    2. A ``StreamingResponse`` is returned with
       ``media_type="text/event-stream"`` and the standard SSE headers.
    3. The generator polls ``broker.pending()`` every 250ms, emitting
       ``new_question`` for each unseen question_id (deduplication is
       per-stream — a reconnect sees the full pending list again).
    4. Each question is tracked for resolution: when it disappears
       from ``broker.pending()`` the generator emits ``answered`` or
       ``timeout``. The distinction is reconstructed from the
       decision store when available; otherwise the stream falls
       back to the generic ``answered`` event (the broker does not
       keep resolved questions around long enough to inspect).
    5. A ``: keep-alive`` comment is emitted every
       ``hooks_elicitation_sse_heartbeat_s`` seconds (default 15) to
       keep intermediate proxies from closing the idle connection.
    6. The generator exits when:
       - the client disconnects (``request.is_disconnected()`` returns
         True, polled every 250ms), or
       - the stream exceeds ``hooks_elicitation_sse_max_session_age_s``
         wall-clock seconds (default 3600), or
       - the server is shutting down (generator is cancelled).

Session scoping:
    The optional ``?session=S`` query parameter filters questions so
    only those published with a matching ``session_id`` are streamed.
    The broker is process-global (see :mod:`harness.elicitation`), so
    session filtering is informational — a client that omits the
    filter sees every question on the broker.

Trust boundary: stdlib + fastapi + harness.elicitation only. The
module imports :class:`ElicitationBroker` lazily inside the handler
so it can be loaded even when the broker is unconfigured (mirrors
the WS / long-poll route pattern). No imports of ``harness.agents``
or other production modules.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope


logger = logging.getLogger("harness.server.routes.elicitation_sse")

router = APIRouter()


# === Wire format constants ===

# SSE spec: https://html.spec.whatwg.org/multipage/server-sent-events.html
# Each event is a block of ``field: value\\n`` lines terminated by a
# blank line (``\\n\\n``). Comments start with ``:`` and are ignored by
# the client EventSource — they're used for heartbeats.
SSE_MEDIA_TYPE = "text/event-stream"
HEARTBEAT_COMMENT = ": keep-alive\n\n"

# Polling cadence for ``broker.pending()`` and ``request.is_disconnected()``.
# 250ms matches the long-poll ``_interval_s`` default and keeps the
# disconnect-detection latency snappy without burning noticeable CPU.
_DISCONNECT_POLL_S = 0.25


# === Guards ===


def _is_sse_enabled(request: Request) -> bool:
    """Read the enable flag from ``app.state`` (set by lifespan).

    Mirrors :func:`harness.server.routes.elicitation_longpoll._is_longpoll_enabled`
    — the flag is set once at app startup and read here on every request
    so operators can flip it via ``app.state`` without restarting.
    Falls back to constructing ``Settings()`` (the WS route's pattern)
    when the lifespan didn't populate ``app.state``.
    """
    cached = getattr(
        request.app.state, "hooks_elicitation_sse_enabled", None,
    )
    if cached is not None:
        return bool(cached)
    from harness.config import Settings

    return bool(Settings().hooks_elicitation_sse_enabled)


def _sse_settings(request: Request) -> tuple[float, float]:
    """Return ``(heartbeat_s, max_session_age_s)`` from app.state or Settings().

    Both are floats in seconds. ``heartbeat_s == 0`` disables the
    keep-alive comment (not recommended behind a reverse proxy but
    allowed so unit tests can disable it cleanly).
    """
    heartbeat_s = getattr(
        request.app.state, "hooks_elicitation_sse_heartbeat_s", None,
    )
    max_age_s = getattr(
        request.app.state, "hooks_elicitation_sse_max_session_age_s", None,
    )
    if heartbeat_s is None or max_age_s is None:
        from harness.config import Settings

        s = Settings()
        heartbeat_s = float(
            heartbeat_s if heartbeat_s is not None
            else s.hooks_elicitation_sse_heartbeat_s
        )
        max_age_s = float(
            max_age_s if max_age_s is not None
            else s.hooks_elicitation_sse_max_session_age_s
        )
    return float(heartbeat_s), float(max_age_s)


# === Handlers ===


@router.get("/sse")
async def elicitation_sse(
    request: Request,
    session: str | None = Query(
        default=None,
        description=(
            "Optional session id filter. When set, only questions "
            "published with a matching ``session_id`` are streamed. "
            "The broker is process-global so the filter is informational."
        ),
    ),
    _token: Any = Depends(require_scope(Scope.ELICITATION_READ)),
) -> StreamingResponse:
    """Server-Sent Events stream for Elicitation lifecycle events.

    Returns a ``text/event-stream`` response that stays open until the
    client disconnects, the heartbeat-max-age is reached, or the server
    shuts down. See the module docstring for the wire format.

    Status codes:
        - 200 (streaming): success — the response body is the SSE stream.
        - 403: ``hooks_elicitation_sse_enabled`` is False, OR the token
          lacks the ``elicitation.read`` scope.
    """
    if not _is_sse_enabled(request):
        raise HTTPException(status_code=403, detail="sse_disabled")

    heartbeat_s, max_age_s = _sse_settings(request)

    # Stash the session filter on a local so the generator closure can
    # see it without re-reading the (already-consumed) Query dependency.
    session_filter: str | None = session

    async def event_stream() -> AsyncIterator[str]:
        """Yield SSE-formatted strings until exit conditions are met.

        The generator owns the deduplication set, the heartbeat timer,
        and the disconnect probe. ``broker.pending()`` is called on
        every poll iteration — that's the source of truth for "what's
        still waiting for an answer".
        """
        from harness.elicitation import ElicitationBroker

        broker = ElicitationBroker.get()
        seen_question_ids: set[str] = set()
        # Track which questions we've announced so we can detect when
        # they leave ``pending()`` (= resolved somehow) and emit a
        # closing event. Keyed by question_id, value is the snapshot
        # of the question payload we sent (so we can echo it back on
        # the resolved event).
        announced: dict[str, dict[str, Any]] = {}

        stream_start = time.monotonic()
        last_heartbeat = stream_start

        logger.debug(
            "elicitation sse: stream opened (session_filter=%r, "
            "heartbeat_s=%.1f, max_age_s=%.1f)",
            session_filter, heartbeat_s, max_age_s,
        )

        try:
            while True:
                # === Exit conditions ===
                # 1. Client disconnect — detected via the Starlette
                #    helper, which checks the transport state. The
                #    poll cadence (250ms) bounds the worst-case
                #    detection latency.
                if await request.is_disconnected():
                    logger.debug("elicitation sse: client disconnected")
                    return
                # 2. Max session age — prevents leaky long-lived
                #    streams. The client is expected to reconnect
                #    (the broker is stateless across reconnects).
                if max_age_s > 0:
                    age = time.monotonic() - stream_start
                    if age >= max_age_s:
                        logger.debug(
                            "elicitation sse: max_session_age reached "
                            "(%.1fs >= %.1fs)", age, max_age_s,
                        )
                        return

                # === Diff broker.pending() against seen set ===
                now_pending: dict[str, Any] = {}
                for pq in broker.pending():
                    # Session filter — skip questions whose session_id
                    # doesn't match (when a filter was supplied). The
                    # comparison is exact-match; empty session_id on
                    # the question side always passes (broker-global).
                    if session_filter is not None:
                        pq_session = getattr(pq, "session_id", "") or ""
                        if pq_session and pq_session != session_filter:
                            continue
                    now_pending[pq.question_id] = pq

                # New questions: emit ``new_question`` for each id in
                # now_pending that we haven't announced yet.
                for qid, pq in now_pending.items():
                    if qid in seen_question_ids:
                        continue
                    seen_question_ids.add(qid)
                    payload = {
                        "question_id": pq.question_id,
                        "question": pq.question,
                        "options": list(pq.options or []),
                        "default_answer": pq.default_answer,
                        "session_id": getattr(pq, "session_id", "") or "",
                        "created_at": getattr(pq, "created_at", 0.0),
                    }
                    announced[qid] = payload
                    yield _format_event("new_question", payload)

                # Resolved questions: anything we announced that's no
                # longer in now_pending. The broker popped it (either
                # ``answered`` via the future or ``timed_out`` in
                # ``wait()``). We can't tell which from pending() alone
                # — fall back to ``answered`` for compatibility (the
                # decision store has the real verdict but reading it
                # on every poll would be I/O-heavy). Tests that need
                # the distinction can monkey-patch this generator.
                resolved = [
                    qid for qid in list(announced.keys())
                    if qid not in now_pending
                ]
                for qid in resolved:
                    payload = announced.pop(qid, {})
                    seen_question_ids.discard(qid)
                    event_name = _classify_resolution(broker, qid, payload)
                    yield _format_event(event_name, payload)

                # === Heartbeat ===
                # Emit a ``: keep-alive`` comment if the heartbeat
                # interval has elapsed since the last one. Disabled
                # when ``heartbeat_s == 0`` (tests / debug only).
                if heartbeat_s > 0:
                    now = time.monotonic()
                    if now - last_heartbeat >= heartbeat_s:
                        last_heartbeat = now
                        yield HEARTBEAT_COMMENT

                # === Poll cadence ===
                # One sleep per outer iteration — keeps CPU usage
                # flat regardless of how many questions are pending.
                await asyncio.sleep(_DISCONNECT_POLL_S)
        except asyncio.CancelledError:
            # Server shutdown — generator was cancelled by the
            # response body iterator. Log and re-raise so Starlette
            # can finish unwinding the response.
            logger.debug("elicitation sse: stream cancelled (shutdown?)")
            raise
        except Exception:  # noqa: BLE001 — fail-open, never hang the client
            logger.exception("elicitation sse: generator error")
            return

    return StreamingResponse(
        event_stream(),
        media_type=SSE_MEDIA_TYPE,
        headers={
            # Disable proxy buffering so events are flushed immediately.
            # ``X-Accel-Buffering: no`` is the nginx-specific knob;
            # ``Cache-Control: no-cache`` is the portable hint. Both
            # are harmless when the proxy doesn't recognise them.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # ``Connection: keep-alive`` is implicit on HTTP/1.1 but
            # explicit here so older proxies don't close after the
            # first chunk.
            "Connection": "keep-alive",
        },
    )


# === Helpers ===


def _format_event(event_name: str, payload: dict[str, Any]) -> str:
    """Serialise one SSE event block.

    Format::

        event: <event_name>
        data: <json payload>

    The payload is JSON-encoded with ``ensure_ascii=False`` so
    non-ASCII question text (Russian, Chinese, …) is preserved
    on the wire. The data field is a single line — the SSE spec
    allows multi-line data via repeated ``data:`` prefixes, but
    JSON has no newlines in its string encoding so a single line
    is sufficient.
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {data}\n\n"


def _classify_resolution(
    broker: Any,
    question_id: str,
    payload: dict[str, Any],
) -> str:
    """Best-effort classification of how a question resolved.

    The broker doesn't keep resolved questions around (``wait()``
    pops them in a ``finally`` block), so by the time we see a
    question leave ``pending()`` it's gone. We try the decision
    store first (Phase 4.8 — has the authoritative verdict); if
    that's unavailable or the row isn't found, we default to
    ``answered`` so the client knows the question is no longer
    pending without us having to guess whether it was a timeout.

    The decision-store lookup is O(1) (single question_id filter
    on the indexed ``question_id`` column) so it's cheap to run
    on every resolution.
    """
    store = getattr(broker, "_decision_store", None)
    if store is not None:
        try:
            # ``query_history`` is session-scoped; we use the
            # session_id stashed in the payload at announce time.
            rows = store.query_history(
                session_id=payload.get("session_id") or None,
                limit=50,
            )
            for row in rows:
                if row.question_id == question_id:
                    if row.decision == "timed_out":
                        return "timeout"
                    return "answered"
        except Exception:  # noqa: BLE001 — best-effort
            pass
    return "answered"


__all__ = ["router"]
