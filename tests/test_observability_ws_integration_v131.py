"""WI-06 — Observability WebSocket integration tests (v1.31).

Covers:
  1. Valid token → 101 Switching Protocols + receive metrics within 2s
  2. Invalid token → WS close code 4001 (or HTTP 401)
  3. Multiple subscribers → all receive same broadcast

Uses ``starlette.testclient.TestClient`` with ``websocket_connect``.
Real broker connections (no mocks for connect); mock timing via
``threading.Event`` / ``time.sleep`` only where unavoidable.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from harness.observability.metrics_broker import MetricsBroker
from harness.server.auth.tokens import TokenRecord


# ---------------------------------------------------------------------------
# Helpers — mirrors test_observability_ws_v131.py pattern
# ---------------------------------------------------------------------------

class _MockTokenStore:
    """A duck-typed token store for testing WS auth."""

    def __init__(self, valid_tokens: set[str] | None = None):
        self._valid = valid_tokens or set()

    async def lookup(self, plaintext: str):
        if plaintext in self._valid:
            return MagicMock(spec=TokenRecord)
        return None


def _make_app(
    broker: MetricsBroker | None = None,
    token_store: _MockTokenStore | None = None,
    auth_required: bool = True,
) -> FastAPI:
    """Build a minimal FastAPI app with the WS route + broker + token store."""
    app = FastAPI()
    app.state.metrics_broker = broker
    app.state.auth_required = auth_required
    app.state.token_store = token_store or _MockTokenStore()

    from harness.server.routes.observability_ws import router

    app.include_router(router)
    return app


def _connect_ws(app: FastAPI, token: str) -> TestClient.websocket_connect:
    """Connect to the WS endpoint with the given token (context manager)."""
    if token:
        return TestClient(app).websocket_connect(
            f"/api/v1/observability/ws?token={token}"
        )
    return TestClient(app).websocket_connect("/api/v1/observability/ws")


# ---------------------------------------------------------------------------
# 1. Valid token → 101 + metrics within 2s
# ---------------------------------------------------------------------------

def test_ws_connect_valid_token() -> None:
    """Valid token → 101 Switching Protocols + receive metrics/health within 2s.

    On connect, the WS loop subscribes to ``["metrics", "health"]`` and
    immediately begins polling the broker.  We publish a metrics message
    and verify the client receives it within the timeout window.
    """
    broker = MetricsBroker(max_backlog=10)
    ts = _MockTokenStore({"good-token"})
    app = _make_app(broker=broker, token_store=ts, auth_required=True)

    with _connect_ws(app, token="good-token") as ws:
        # Verify handshake succeeded by sending a ping.
        ws.send_text(json.dumps({"type": "ping"}))
        resp = ws.receive_json()
        assert resp["type"] == "pong", f"Expected pong, got {resp}"

        # Publish a metrics message into the broker (needs an event loop).
        async def _pub() -> None:
            await asyncio.sleep(0.05)
            await broker.publish("metrics", {"cpu": 0.42, "memory_mb": 512})

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_pub())
        loop.close()

        # The WS writer polls every 0.5s — message should arrive within 2s.
        msg = ws.receive_json()
        assert msg is not None, "No message received within timeout"
        assert msg["type"] in ("metrics", "health"), (
            f"Expected metrics or health, got {msg['type']!r}"
        )
        if msg["type"] == "metrics":
            assert "data" in msg


# ---------------------------------------------------------------------------
# 2. Invalid token → 4001 (or HTTP 401)
# ---------------------------------------------------------------------------

def test_ws_connect_invalid_token() -> None:
    """Invalid token → server closes with code 4001 (or HTTP 401)."""
    broker = MetricsBroker(max_backlog=10)
    ts = _MockTokenStore({"good-token"})
    app = _make_app(broker=broker, token_store=ts, auth_required=True)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _connect_ws(app, token="bad-token") as ws:
            ws.receive_json()

    assert exc_info.value.code in (4001, 401), (
        f"Expected close code 4001 or 401, got {exc_info.value.code}"
    )


# ---------------------------------------------------------------------------
# 3. Multiple subscribers → broadcast
# ---------------------------------------------------------------------------

def test_ws_multiple_subscribers_broadcast() -> None:
    """Multiple concurrent subscribers → all receive the same broadcast.

    Two WebSocket clients connect, both subscribe to ``["metrics"]`` by
    default.  When a message is published to the broker, both clients
    receive it independently.
    """
    broker = MetricsBroker(max_backlog=10)
    ts = _MockTokenStore({"t"})
    app = _make_app(broker=broker, token_store=ts, auth_required=True)

    # Collect received messages per client.
    results: dict[str, list[dict[str, Any]]] = {"ws1": [], "ws2": []}
    errors: list[str] = []
    ready = threading.Event()
    start_barrier = threading.Barrier(2, timeout=10)

    def _ws_worker(label: str) -> None:
        try:
            with _connect_ws(app, token="t") as ws:
                # Verify connection.
                ws.send_text(json.dumps({"type": "ping"}))
                resp = ws.receive_json()
                assert resp["type"] == "pong", f"{label}: pong expected"

                # Signal that we're connected and ready.
                ready.set()
                start_barrier.wait()

                # Now wait for the broadcast — timeout 5s.
                for _ in range(3):
                    try:
                        msg = ws.receive_json()
                        if msg and msg.get("type") in ("metrics", "health"):
                            results[label].append(msg)
                    except WebSocketDisconnect:
                        break
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    t1 = threading.Thread(target=_ws_worker, args=("ws1",), daemon=True)
    t2 = threading.Thread(target=_ws_worker, args=("ws2",), daemon=True)
    t1.start()
    t2.start()

    # Wait for at least one client to be connected.
    ready.wait(timeout=10)

    # Give both clients a moment to finish their WS handshake + subscribe.
    time.sleep(0.5)

    # Publish a broadcast message.
    async def _broadcast() -> None:
        await asyncio.sleep(0.05)
        await broker.publish("metrics", {"cpu": 0.99, "event": "broadcast_test"})

    loop = asyncio.new_event_loop()
    delivered = loop.run_until_complete(_broadcast())
    loop.close()

    t1.join(timeout=8)
    t2.join(timeout=8)

    # Check for errors.
    assert not errors, f"Worker errors: {errors}"

    # Both clients should have received the metrics broadcast.
    assert len(results["ws1"]) > 0, (
        f"ws1 received no metrics broadcast (delivered={delivered})"
    )
    assert len(results["ws2"]) > 0, (
        f"ws2 received no metrics broadcast (delivered={delivered})"
    )

    # Verify both received the same data payload.
    ws1_data = [m.get("data") for m in results["ws1"] if m.get("data")]
    ws2_data = [m.get("data") for m in results["ws2"] if m.get("data")]
    assert ws1_data, "ws1: no data in messages"
    assert ws2_data, "ws2: no data in messages"

    # At least one matching data point between the two clients.
    common = [d for d in ws1_data if d in ws2_data]
    assert common, (
        f"No common data between ws1 {ws1_data} and ws2 {ws2_data}"
    )
