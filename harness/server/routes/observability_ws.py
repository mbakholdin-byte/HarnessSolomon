"""WI-04: Observability WebSocket endpoint.

``GET /api/v1/observability/ws?token=<bearer>``

- Auth: Bearer token from query param (WebSocket can't set headers).
- Server → client: ``{type: "metrics"|"health", data: {...}}`` every 1s.
- Client → server: ``{type: "subscribe", topics: [...]}``, ``{type: "ping"}``.
- Heartbeat: disconnect if no ping for 30s.

Trust boundary: this module imports from harness.observability (broker),
harness.config (settings), and harness.server.auth (TokenStore). It does
NOT import from harness.agents.*.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect, status

from harness.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["observability-ws"])

# Default config values (overridable via settings).
_WS_HEARTBEAT_S: float = 30.0
_WS_BACKLOG: int = 100


def _get_broker(request: Request) -> Any:
    """Pull the :class:`MetricsBroker` from ``app.state``.

    Returns ``None`` when the broker is not configured (the route
    then returns 503).
    """
    return getattr(request.app.state, "metrics_broker", None)


def _get_token_store(request: Request) -> Any:
    """Pull the token store from ``app.state``."""
    return getattr(request.app.state, "token_store", None)


def _is_auth_required(request: Request) -> bool:
    """Read the auth-required flag from app.state."""
    return bool(getattr(request.app.state, "auth_required", True))


async def _validate_token(token_str: str, request: Request) -> bool:
    """Validate a Bearer token string against the token store.

    Returns True if the token is valid, False otherwise.
    In open dev mode (auth_required=False), always returns True.
    """
    if not _is_auth_required(request):
        return True
    if not token_str:
        return False
    store = _get_token_store(request)
    if store is None:
        logger.warning("observability_ws: token_store not initialised")
        return False
    record = await store.lookup(token_str)
    return record is not None


@router.websocket("/api/v1/observability/ws")
async def observability_ws(
    websocket: WebSocket,
    token: str = Query(default=""),
) -> None:
    """WebSocket endpoint for real-time observability data.

    Query params:
        token: Bearer token (plaintext, not hex-encoded). Validated
               against the TokenStore. In open dev mode the check is
               bypassed.

    Server → client messages:
        ``{type: "metrics", data: {...}}``  — metrics snapshot (every 1s)
        ``{type: "health", data: {...}}``   — health report (every 1s)
        ``{type: "pong"}``                  — response to client ping

    Client → server messages:
        ``{type: "subscribe", topics: [...]}``  — change topic filter
        ``{type: "ping"}``                      — keepalive

    Heartbeat: if no ``ping`` received for ``ws_heartbeat_s`` seconds,
    the connection is closed with code 4001.
    """
    # --- Auth ---
    if not await _validate_token(token, websocket):
        await websocket.close(code=4001, reason="invalid token")
        return

    await websocket.accept()

    broker = _get_broker(websocket)
    if broker is None:
        await websocket.send_json({"type": "error", "detail": "metrics_broker not configured"})
        await websocket.close(code=1011)
        return

    # Read config (use defaults if settings not available).
    heartbeat_s = float(getattr(settings, "ws_heartbeat_s", _WS_HEARTBEAT_S))

    # Unique per-connection session id.
    session_id = f"ws-{id(websocket)}"

    # Default topics on connect.
    await broker.subscribe(session_id, ["metrics", "health"])
    logger.info("observability_ws: %s connected", session_id)

    try:
        await _ws_loop(
            websocket=websocket,
            broker=broker,
            session_id=session_id,
            heartbeat_s=heartbeat_s,
        )
    finally:
        await broker.unsubscribe(session_id)
        logger.info("observability_ws: %s disconnected", session_id)


async def _ws_loop(
    websocket: WebSocket,
    broker: Any,
    session_id: str,
    heartbeat_s: float,
) -> None:
    """Main WebSocket read/write loop.

    Two concurrent tasks:
      1. ``_reader`` — reads client messages (subscribe / ping).
      2. ``_writer`` — polls broker and sends to client.

    The loop terminates when either task raises or the heartbeat expires.
    """
    last_ping: float = asyncio.get_event_loop().time()

    async def _reader() -> None:
        nonlocal last_ping
        while True:
            raw = await websocket.receive_text()
            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "invalid JSON"})
                continue

            msg_type = msg.get("type", "")
            if msg_type == "ping":
                last_ping = asyncio.get_event_loop().time()
                await websocket.send_json({"type": "pong"})
            elif msg_type == "subscribe":
                topics = msg.get("topics", [])
                if isinstance(topics, list) and topics:
                    await broker.subscribe(session_id, [str(t) for t in topics])
                    await websocket.send_json({
                        "type": "subscribed",
                        "topics": topics,
                    })
                else:
                    await websocket.send_json({
                        "type": "error",
                        "detail": "topics must be a non-empty list of strings",
                    })
            else:
                await websocket.send_json({
                    "type": "error",
                    "detail": f"unknown message type: {msg_type!r}",
                })

    async def _writer() -> None:
        while True:
            msg = await broker.recv(session_id, timeout=0.5)
            if msg is not None:
                await websocket.send_json(msg)

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(1.0)
            now = asyncio.get_event_loop().time()
            if now - last_ping > heartbeat_s:
                logger.warning(
                    "observability_ws: %s heartbeat timeout (last ping %.1fs ago)",
                    session_id, now - last_ping,
                )
                await websocket.close(code=4001, reason="heartbeat timeout")
                return

    # Run reader + writer + heartbeat concurrently.
    # When any task completes (or raises), we cancel the others.
    reader_task = asyncio.create_task(_reader())
    writer_task = asyncio.create_task(_writer())
    heartbeat_task = asyncio.create_task(_heartbeat())

    done, pending = await asyncio.wait(
        [reader_task, writer_task, heartbeat_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel remaining tasks.
    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    # Re-raise any exception from the completed task (except CancelledError).
    for t in done:
        exc = t.exception()
        if exc is not None and not isinstance(exc, (asyncio.CancelledError, WebSocketDisconnect)):
            logger.exception("observability_ws: %s task failed", session_id)


__all__ = ["router"]
