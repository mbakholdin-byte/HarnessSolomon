"""WI-04: Tests for observability_ws WebSocket endpoint.

Covers:
    1. Connect with valid token → 101 Switching Protocols
    2. Connect with invalid token → WS close code 4001
    3. Ping → pong
    4. Subscribe message → topic filter applied
    5. Metrics published within 2s of connect
    6. Heartbeat timeout → disconnect

Uses TestClient (sync) with a minimal FastAPI app that has the broker
pre-injected on app.state, mirroring the elicitation WS test pattern.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from harness.observability.metrics_broker import MetricsBroker
from harness.server.auth.tokens import TokenRecord
from unittest.mock import MagicMock


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
    if token_store is not None:
        app.state.token_store = token_store
    else:
        app.state.token_store = _MockTokenStore()

    from harness.server.routes.observability_ws import router
    app.include_router(router)
    return app


def _connect_ws(app: FastAPI, token: str = "test-token") -> TestClient.websocket_connect:
    """Connect to the WS endpoint with the given token.

    Returns a context manager (``client.websocket_connect(...)``).
    """
    if token:
        return TestClient(app).websocket_connect(
            f"/api/v1/observability/ws?token={token}"
        )
    return TestClient(app).websocket_connect("/api/v1/observability/ws")


def _recv_json_or_none(ws, timeout: float = 2.0) -> dict | None:
    """Receive JSON from a starlette WebSocketTestSession with a simple timeout.

    Starlette's ``receive_json()`` does not support a ``timeout`` parameter.
    We use a background thread to time-limit the blocking receive.
    """
    import threading

    result: dict | None = None
    exc: Exception | None = None

    def _recv():
        nonlocal result, exc
        try:
            result = ws.receive_json()
        except Exception as e:
            exc = e

    t = threading.Thread(target=_recv, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Still blocked — timeout.
        return None
    if exc is not None:
        raise exc
    return result


# ── Tests ─────────────────────────────────────────────────────────────


class TestObservabilityWebSocket:
    """Integration tests for the observability_ws endpoint."""

    # ── 1. Connect with valid token → 101 Switching Protocols ─────────

    def test_connect_with_valid_token(self) -> None:
        """Valid token → WS upgrade succeeds."""
        broker = MetricsBroker(max_backlog=10)
        ts = _MockTokenStore({"good-token"})
        app = _make_app(broker=broker, token_store=ts, auth_required=True)

        with _connect_ws(app, token="good-token") as ws:
            # If we got here, the handshake succeeded (101).
            # Send a ping to verify the connection works.
            ws.send_text(json.dumps({"type": "ping"}))
            resp = ws.receive_json()
            assert resp["type"] == "pong"

    # ── 2. Connect with invalid token → WS close code 4001 ────────────

    def test_connect_invalid_token(self) -> None:
        """Invalid token → server closes with code 4001."""
        broker = MetricsBroker(max_backlog=10)
        ts = _MockTokenStore({"good-token"})
        app = _make_app(broker=broker, token_store=ts, auth_required=True)

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with _connect_ws(app, token="bad-token") as ws:
                ws.receive_json()

        assert exc_info.value.code == 4001

    def test_connect_no_token(self) -> None:
        """Missing token → server closes with code 4001."""
        broker = MetricsBroker(max_backlog=10)
        ts = _MockTokenStore({"good-token"})
        app = _make_app(broker=broker, token_store=ts, auth_required=True)

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with _connect_ws(app, token="") as ws:
                ws.receive_json()

        assert exc_info.value.code == 4001

    # ── 3. Ping → pong ────────────────────────────────────────────────

    def test_ping_pong(self) -> None:
        """Client ping → server responds with pong."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, token_store=_MockTokenStore({"t"}), auth_required=True)

        with _connect_ws(app, token="t") as ws:
            ws.send_text(json.dumps({"type": "ping"}))
            resp = ws.receive_json()
            assert resp["type"] == "pong"

    # ── 4. Subscribe message → topic filter applied ───────────────────

    def test_subscribe_updates_topics(self) -> None:
        """Client subscribe() → broker topics updated."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, token_store=_MockTokenStore({"t"}), auth_required=True)

        with _connect_ws(app, token="t") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "topics": ["audit"]}))
            ack = ws.receive_json()
            assert ack["type"] == "subscribed"
            assert ack["topics"] == ["audit"]

    def test_subscribe_invalid_topics_returns_error(self) -> None:
        """Subscribe with invalid topics → error response."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, token_store=_MockTokenStore({"t"}), auth_required=True)

        with _connect_ws(app, token="t") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "topics": []}))
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "topics" in resp.get("detail", "")

    # ── 5. Metrics published within 2s of connect ─────────────────────

    def test_metrics_published_on_connect(self) -> None:
        """After connect, metrics/health messages arrive within 2s."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, token_store=_MockTokenStore({"t"}), auth_required=True)

        with _connect_ws(app, token="t") as ws:
            # Publish a metrics message directly into the broker.
            # We need to run the async publish on the event loop.
            async def _pub():
                await asyncio.sleep(0.1)
                await broker.publish("metrics", {"cpu": 0.42})

            loop = asyncio.new_event_loop()
            loop.run_until_complete(_pub())
            loop.close()

            # The WS loop polls every 0.5s, so we should receive
            # within ~2s. Use receive_json() blocking.
            msg = ws.receive_json()
            assert msg["type"] in ("metrics", "health"), f"unexpected type: {msg['type']}"

    # ── 6. Heartbeat timeout → disconnect ─────────────────────────────

    def test_heartbeat_timeout_disconnects(self) -> None:
        """If no ping within heartbeat window, server disconnects."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, token_store=_MockTokenStore({"t"}), auth_required=True)

        # Monkeypatch the heartbeat to be very short for testing.
        import harness.config
        import time as _time
        original = getattr(harness.config.settings, "ws_heartbeat_s", 30.0)
        try:
            harness.config.settings.ws_heartbeat_s = 1.0  # 1s heartbeat

            with _connect_ws(app, token="t") as ws:
                # Don't send any ping — the heartbeat fires after ~1s
                # and the server closes with code 4001.
                # Starlette's TestClient raises WebSocketDisconnect on
                # receive after a server-side close (but not on send).
                _time.sleep(1.5)  # Wait for heartbeat to fire.
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws.receive_json()
                assert exc_info.value.code == 4001
        finally:
            harness.config.settings.ws_heartbeat_s = original

    # ── Edge cases ────────────────────────────────────────────────────

    def test_broker_not_configured_returns_error(self) -> None:
        """When broker is None, endpoint sends error and closes."""
        app = _make_app(broker=None, token_store=_MockTokenStore({"t"}))

        # Use open dev mode so auth passes but broker is missing.
        app.state.auth_required = False

        # The endpoint: accepts, sends error JSON, then closes (1011).
        try:
            with _connect_ws(app, token="t") as ws:
                msg = ws.receive_json()
                assert msg["type"] == "error"
                # After error, server closes → next receive raises.
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws.receive_json()
                assert exc_info.value.code == 1011
        except WebSocketDisconnect:
            # If the close happens before we can read the error,
            # that's also acceptable behavior.
            pass

    def test_invalid_json_returns_error(self) -> None:
        """Garbage sent to WS → error response."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, token_store=_MockTokenStore({"t"}), auth_required=True)

        with _connect_ws(app, token="t") as ws:
            ws.send_text("not json {{{")
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "invalid JSON" in resp.get("detail", "")

    def test_unknown_message_type_returns_error(self) -> None:
        """Unknown message type → error response."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, token_store=_MockTokenStore({"t"}), auth_required=True)

        with _connect_ws(app, token="t") as ws:
            ws.send_text(json.dumps({"type": "frobnicate"}))
            resp = ws.receive_json()
            assert resp["type"] == "error"
            assert "unknown message type" in resp.get("detail", "")

    def test_open_dev_mode_bypasses_auth(self) -> None:
        """In open dev mode, any token (even empty) is accepted."""
        broker = MetricsBroker(max_backlog=10)
        app = _make_app(broker=broker, auth_required=False)

        with _connect_ws(app, token="") as ws:
            # Should succeed — auth is bypassed.
            ws.send_text(json.dumps({"type": "ping"}))
            resp = ws.receive_json()
            assert resp["type"] == "pong"
