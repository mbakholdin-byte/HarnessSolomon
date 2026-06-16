"""Phase 4.0: Tests for HTTP transport (urllib-based)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from harness.hooks.context import HookContext
from harness.hooks.http import invoke_http_hook


@pytest.fixture
def ctx() -> HookContext:
    return HookContext(
        event="PreToolUse",
        session_id="s1",
        agent_id="",
        payload={"tool_name": "read_file"},
    )


class _MockHandler(BaseHTTPRequestHandler):
    """HTTP server fixture that captures requests and returns canned responses."""

    status_code = 200
    response_body = b'{"decision": "allow", "output": {}}'

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)  # consume body
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass  # silence stderr


@pytest.fixture(autouse=True)
def reset_mock_handler_state():
    """Reset the mock handler's class-level state between tests."""
    _MockHandler.status_code = 200
    _MockHandler.response_body = b'{"decision": "allow", "output": {}}'
    yield
    # Teardown: also reset in case test mutated.
    _MockHandler.status_code = 200
    _MockHandler.response_body = b'{"decision": "allow", "output": {}}'


@pytest.fixture
def http_server():
    """Start a local HTTP server in a background thread; return its URL."""
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


class TestHTTPSuccess:
    """HTTP transport handles 2xx responses with JSON decision."""

    async def test_simple_allow(self, http_server, ctx) -> None:
        d = await invoke_http_hook(
            f"{http_server}/hook", ctx, timeout_ms=5000
        )
        assert d.decision == "allow"
        assert d.hook_id.startswith("http.")

    async def test_block_with_reason(self, http_server, ctx) -> None:
        _MockHandler.status_code = 200
        _MockHandler.response_body = b'{"decision": "block", "output": {"reason": "denied"}}'
        d = await invoke_http_hook(
            f"{http_server}/hook", ctx, timeout_ms=5000
        )
        assert d.decision == "block"
        assert d.output == {"reason": "denied"}

    async def test_modify_with_payload(self, http_server, ctx) -> None:
        _MockHandler.response_body = (
            b'{"decision": "modify", "output": {"payload": {"k": "v"}}}'
        )
        d = await invoke_http_hook(
            f"{http_server}/hook", ctx, timeout_ms=5000
        )
        assert d.decision == "modify"
        assert d.output == {"payload": {"k": "v"}}


class TestHTTPErrors:
    """HTTP transport fails open on 4xx/5xx + invalid JSON."""

    async def test_500_fails_open(self, http_server, ctx) -> None:
        _MockHandler.status_code = 500
        _MockHandler.response_body = b"internal error"
        d = await invoke_http_hook(
            f"{http_server}/hook", ctx, timeout_ms=5000
        )
        assert d.decision == "allow"
        assert "HTTP 500" in d.error

    async def test_invalid_json_fails_open(self, http_server, ctx) -> None:
        _MockHandler.response_body = b"not json"
        d = await invoke_http_hook(
            f"{http_server}/hook", ctx, timeout_ms=5000
        )
        assert d.decision == "allow"
        assert "invalid JSON" in d.error

    async def test_empty_response(self, http_server, ctx) -> None:
        _MockHandler.response_body = b""
        d = await invoke_http_hook(
            f"{http_server}/hook", ctx, timeout_ms=5000
        )
        assert d.decision == "allow"
        assert "empty" in d.error


class TestHTTPNetworkErrors:
    """HTTP transport fails open on connection errors + timeout."""

    async def test_unreachable_host(self, ctx) -> None:
        # Reserved testnet address, should always be unreachable.
        d = await invoke_http_hook(
            "https://this-host-does-not-exist.invalid/hook",
            ctx,
            timeout_ms=500,
        )
        assert d.decision == "allow"
        assert d.error != ""

    async def test_timeout(self, http_server, ctx) -> None:
        # Override handler to sleep so we trigger timeout.
        original_do_POST = _MockHandler.do_POST

        def slow_post(self):  # noqa: ANN001
            import time

            time.sleep(5)
            original_do_POST(self)

        _MockHandler.do_POST = slow_post  # type: ignore[method-assign]
        try:
            d = await invoke_http_hook(
                f"{http_server}/hook", ctx, timeout_ms=100
            )
            assert d.decision == "allow"  # fail-open
            assert "timeout" in d.error
        finally:
            _MockHandler.do_POST = original_do_POST  # type: ignore[method-assign]


class TestHTTPWireFormat:
    """HTTP request body is well-formed JSON matching HookContext."""

    async def test_receives_full_context(self, http_server) -> None:
        received: list[dict] = []

        def capture(self):  # noqa: ANN001
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            received.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "27")
            self.end_headers()
            self.wfile.write(b'{"decision": "allow", "output": {}}')

        original = _MockHandler.do_POST
        _MockHandler.do_POST = capture  # type: ignore[method-assign]
        try:
            ctx = HookContext(
                event="PreToolUse",
                session_id="s-1",
                agent_id="a-1",
                payload={"tool_name": "write_file"},
            )
            d = await invoke_http_hook(
                f"{http_server}/hook", ctx, timeout_ms=5000
            )
            assert d.decision == "allow"
            assert len(received) == 1
            assert received[0]["event"] == "PreToolUse"
            assert received[0]["session_id"] == "s-1"
            assert received[0]["payload"]["tool_name"] == "write_file"
        finally:
            _MockHandler.do_POST = original  # type: ignore[method-assign]

    async def test_auth_header_passed(self, http_server) -> None:
        seen_auth: list[str] = []

        def capture(self):  # noqa: ANN001
            seen_auth.append(self.headers.get("Authorization", ""))
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "27")
            self.end_headers()
            self.wfile.write(b'{"decision": "allow", "output": {}}')

        original = _MockHandler.do_POST
        _MockHandler.do_POST = capture  # type: ignore[method-assign]
        try:
            ctx = HookContext(
                event="PreToolUse", session_id="s1", agent_id="", payload={}
            )
            d = await invoke_http_hook(
                f"{http_server}/hook",
                ctx,
                timeout_ms=5000,
                headers={"Authorization": "Bearer test-token"},
            )
            assert d.decision == "allow"
            assert seen_auth == ["Bearer test-token"]
        finally:
            _MockHandler.do_POST = original  # type: ignore[method-assign]
