"""Phase 4.3+ v1.12.0: Elicitation WebSocket endpoint.

Endpoint: ``/api/v1/elicitation/ws``

Protocol (client → server):
    - First message: ``{"action": "subscribe"}`` — server starts
      pushing pending questions as they appear.
    - Server pushes: ``{"action": "question", "question_id": "...",
      "question": "...", "options": [...], "default_answer": "..."}``
    - Client answers: ``{"action": "answer", "question_id": "...",
      "value": "proceed"}`` — server resolves the future.
    - Client can also send ``{"action": "list"}`` to get a snapshot
      of all currently pending questions.

v1.0.0 security fix: WS upgrade requires scope ``elicitation.write``
(answer Elicitation questions, including confirm_dangerous). Token is
passed via ``Authorization: Bearer <token>`` header on the upgrade
request (WebSocket protocol). Falls back to ``?token=<token>`` query
parameter for clients that cannot set headers (browser WS API).

Trust boundary: stdlib + fastapi only. NO imports of ``harness.agents``
or other production modules.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


logger = logging.getLogger("harness.server.routes.elicitation")

router = APIRouter()


@router.websocket("/ws")
async def elicitation_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint for interactive Elicitation.

    The endpoint stays open: the server pushes questions as they
    arrive (via polling ``broker.pending()`` every 500ms) and
    accepts answers from the client.

    v1.0.0: requires ``elicitation.write`` scope. Connection is rejected
    with code 1008 (policy violation) if the token lacks the scope.
    """
    # Lazy import to keep this route import-clean for non-WebSocket use.
    from harness.config import Settings
    from harness.elicitation import ElicitationBroker
    from harness.server.auth.scopes import Scope
    from harness.server.auth.tokens import TokenStore

    settings = Settings()
    if not settings.hooks_elicitation_ws_enabled:
        await websocket.close(code=1008, reason="Elicitation WS disabled")
        return

    # v1.0.0 security fix: enforce elicitation.write scope on WS upgrade.
    # Accept token via Authorization header (preferred) or ?token= query (fallback).
    token = _extract_ws_token(websocket)
    if token is None:
        await websocket.close(code=1008, reason="missing Bearer token")
        return
    # Use the singleton TokenStore from app.state (lifespan-managed).
    store = getattr(websocket.app.state, "token_store", None)
    if store is None:
        await websocket.close(code=1011, reason="auth not initialised")
        return
    record = await store.lookup(token)
    if record is None or not record.is_active:
        await websocket.close(code=1008, reason="invalid or expired token")
        return
    if Scope.ELICITATION_WRITE not in record.scopes:
        await websocket.close(
            code=1008,
            reason=f"missing required scope: {Scope.ELICITATION_WRITE.value}",
        )
        return

    broker = ElicitationBroker.get()
    await websocket.accept()
    try:
        # Send a hello frame so the client knows it's connected.
        await websocket.send_json({
            "action": "connected",
            "stats": broker.stats(),
        })
        # Two concurrent loops: receive loop (client → server) + poll loop
        # (server → client). They run until disconnect.
        receive_task = asyncio.create_task(
            _receive_loop(websocket, broker), name="elicitation-recv"
        )
        poll_task = asyncio.create_task(
            _poll_loop(websocket, broker), name="elicitation-poll"
        )
        done, pending = await asyncio.wait(
            {receive_task, poll_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # Drain cancelled tasks.
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    except WebSocketDisconnect:
        logger.debug("Elicitation WS: client disconnected")
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("Elicitation WS error (%s): %s", type(e).__name__, e)
        try:
            await websocket.close(code=1011, reason=f"server error: {e}")
        except Exception:  # noqa: BLE001
            pass


def _extract_ws_token(websocket: WebSocket) -> str | None:
    """Extract Bearer token from WS upgrade request.

    Priority:
        1. ``Authorization: Bearer <token>`` header (preferred, RFC 7235)
        2. ``?token=<token>`` query parameter (browser fallback)
    """
    # Header path
    auth = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    # Query parameter fallback
    return websocket.query_params.get("token")


async def _receive_loop(websocket: WebSocket, broker: "Any") -> None:
    """Process client → server messages (subscribe/answer/list)."""
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send_json({"action": "error", "error": "invalid JSON"})
            continue
        action = msg.get("action", "")
        if action == "answer":
            qid = msg.get("question_id", "")
            value = msg.get("value", "")
            ok = broker.answer(qid, value)
            await websocket.send_json({
                "action": "answer_ack",
                "question_id": qid,
                "accepted": ok,
            })
        elif action == "list":
            await websocket.send_json({
                "action": "pending",
                "questions": [
                    {
                        "question_id": pq.question_id,
                        "question": pq.question,
                        "options": pq.options,
                        "default_answer": pq.default_answer,
                    }
                    for pq in broker.pending()
                ],
            })
        elif action == "ping":
            await websocket.send_json({"action": "pong", "stats": broker.stats()})
        else:
            await websocket.send_json({
                "action": "error",
                "error": f"unknown action: {action!r}",
            })


async def _poll_loop(websocket: WebSocket, broker: "Any") -> None:
    """Push new questions to the client every 500ms.

    Diff-based: track the set of question_ids we've already pushed and
    only send ones we haven't seen yet.
    """
    seen: set[str] = set()
    while True:
        await asyncio.sleep(0.5)
        for pq in broker.pending():
            if pq.question_id in seen:
                continue
            seen.add(pq.question_id)
            try:
                await websocket.send_json({
                    "action": "question",
                    "question_id": pq.question_id,
                    "question": pq.question,
                    "options": pq.options,
                    "default_answer": pq.default_answer,
                })
            except Exception:  # noqa: BLE001 — connection probably closed
                return
        # Prune seen set for resolved questions.
        live = {pq.question_id for pq in broker.pending()}
        seen &= live
