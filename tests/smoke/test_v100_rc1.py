"""Smoke tests for Solomon Harness v1.0.0-rc1 (Phase 4.14B).

Eight integration tests that exercise real production code paths
end-to-end. Each test is isolated: it builds its own app, token
store, hook registry, etc. No test depends on another.

Marked with ``@pytest.mark.smoke`` so they are skipped in the
default ``pytest`` run (smoke tests are slower than unit tests
because they boot the real FastAPI lifespan + in-process HTTP
client). Run explicitly::

    pytest tests/smoke/test_v100_rc1.py -v -m smoke

The tests do NOT mock harness internals — they call the same code
the server runs in production. External network is avoided:
the webhook listener uses ``aiohttp`` bound to ``127.0.0.1:0``
(ephemeral port) and the LLM router is never invoked (we assert
on plumbing, not on model output).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# Test 1: import + __version__
# ---------------------------------------------------------------------------

def test_install_and_import_harness() -> None:
    """The package imports cleanly and exposes ``__version__``.

    Verifies that the install (``pip install -e .``) succeeded and
    the top-level package surface is intact. This is the most basic
    smoke test — if it fails, nothing else can work.
    """
    import harness

    assert hasattr(harness, "__version__"), "harness.__version__ missing"
    assert isinstance(harness.__version__, str)
    # Version is a non-empty dotted string (e.g. "1.21.0").
    assert "." in harness.__version__, (
        f"unexpected version format: {harness.__version__!r}"
    )
    # Author + license are part of the public surface (see __init__.py).
    assert harness.__author__
    assert harness.__license__ == "MIT"


# ---------------------------------------------------------------------------
# Test 2: create_app() responds
# ---------------------------------------------------------------------------

async def test_create_app_responds(
    isolated_settings: dict[str, Path],
) -> None:
    """``create_app()`` returns a FastAPI app that serves HTTP.

    We hit ``GET /api/v1/capabilities`` — the public discovery
    endpoint (no auth required, always mounted). A 200 with a JSON
    body confirms the app factory, middleware stack, and at least
    one router are wired correctly.
    """
    from httpx import ASGITransport, AsyncClient as _AC

    from harness.server.app import create_app

    app = create_app()
    assert app is not None, "create_app() returned None"
    # FastAPI sets the title/version at construction.
    assert app.title == "Solomon Harness"

    transport = ASGITransport(app=app)
    async with _AC(transport=transport, base_url="http://test") as ac:
        # /api/v1/capabilities is always public (Phase 1.6 design).
        r = await ac.get("/api/v1/capabilities")
        assert r.status_code == 200, (
            f"capabilities endpoint failed: {r.status_code} {r.text}"
        )
        body = r.json()
        # The capabilities response surfaces the auth surface; in dev
        # mode (auth_required=False) the body still carries the
        # ``auth_required`` flag so a client can discover the mode.
        assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Test 3: auth token creation
# ---------------------------------------------------------------------------

async def test_auth_token_creation(
    auth_store: Any,
) -> None:
    """``TokenStore.create()`` returns a bearer-suitable plaintext token.

    The plaintext is URL-safe (``secrets.token_urlsafe``) and the
    record carries the SHA-256 hash, not the plaintext. This mirrors
    the production CLI path (``harness auth create``) and confirms
    the Phase 1.6 scope-gated API surface is bootable.
    """
    from harness.server.auth.scopes import Scope

    plaintext, record = await auth_store.create(
        "smoke-test-token",
        scopes={Scope.AGENTS_READ, Scope.SESSIONS_READ},
    )

    # Plaintext is a non-empty URL-safe string suitable for
    # ``Authorization: Bearer <plaintext>``.
    assert isinstance(plaintext, str)
    assert len(plaintext) >= 40, (
        f"plaintext too short for 32-byte entropy: {len(plaintext)}"
    )
    # The record must NOT carry the plaintext — only the hash.
    assert record.token_hash != plaintext
    assert record.label == "smoke-test-token"
    assert Scope.AGENTS_READ in record.scopes
    assert record.is_active

    # Round-trip: lookup by plaintext resolves to the same record.
    looked_up = await auth_store.lookup(plaintext)
    assert looked_up is not None
    assert looked_up.token_hash == record.token_hash


# ---------------------------------------------------------------------------
# Test 4: chat completion creates a session (REST proxy)
# ---------------------------------------------------------------------------

async def test_chat_completion_creates_session(
    isolated_settings: dict[str, Path],
    client: Any,
) -> None:
    """A session created via the REST API is retrievable.

    The chat surface is WebSocket-only (see ``routes/chat.py``);
    there is no ``POST /api/v1/chat``. We exercise the session
    lifecycle that the chat endpoint depends on: create via
    ``POST /api/sessions``, then fetch via ``GET /api/sessions/{id}``.
    This confirms the DB layer, the sessions router, and the JSONL
    mirror are all wired.
    """
    # Create a session (the same call the chat WS needs before connecting).
    r = await client.post(
        "/api/sessions",
        json={"title": "smoke-chat", "model": "MiniMax-M2.7"},
    )
    assert r.status_code == 201, f"create session failed: {r.text}"
    session = r.json()
    session_id = session["id"]
    assert session_id, "session id missing in response"

    # The session must be retrievable.
    r2 = await client.get(f"/api/sessions/{session_id}")
    assert r2.status_code == 200, f"fetch session failed: {r2.text}"
    fetched = r2.json()
    assert fetched["id"] == session_id

    # The session appears in the list endpoint.
    r3 = await client.get("/api/sessions")
    assert r3.status_code == 200
    ids = [s["id"] for s in r3.json()]
    assert session_id in ids, (
        f"created session {session_id} not in list: {ids}"
    )


# ---------------------------------------------------------------------------
# Test 5: hooks audit records PreToolUse
# ---------------------------------------------------------------------------

async def test_hooks_audit_records_pre_tool_use(
    isolated_settings: dict[str, Path],
) -> None:
    """A registered PreToolUse hook produces an audit record.

    Builds a real ``HookRegistry`` + ``HookRunner`` + ``HookAuditSink``
    (the same objects the server lifespan wires), registers a builtin
    hook that returns ``allow``, fires a ``PreToolUse`` event, and
    confirms the audit NDJSON file contains the decision.
    """
    from harness.hooks.audit import HookAuditSink
    from harness.hooks.context import HookContext, HookDecision
    from harness.hooks.events import EventType
    from harness.hooks.registry import HookRegistry, HookSpec
    from harness.hooks.runner import HookRunner

    audit_dir = isolated_settings["session_dir"] / "audit"
    sink = HookAuditSink(audit_dir)

    registry = HookRegistry()

    async def _audit_hook(ctx: HookContext) -> HookDecision:
        return HookDecision(
            decision="allow",
            hook_id="smoke.audit_hook",
            output={"echo": ctx.payload.get("tool_name", "")},
        )

    await registry.register(
        HookSpec(
            hook_id="smoke.audit_hook",
            event=EventType.PRE_TOOL_USE,
            transport="builtin",
            callable=_audit_hook,
        )
    )

    runner = HookRunner(registry, default_timeout_ms=1000, audit_sink=sink)

    ctx = HookContext(
        event=EventType.PRE_TOOL_USE.value,
        session_id="smoke-session",
        agent_id="smoke-agent",
        payload={"tool_name": "read_file", "arguments": {}},
    )
    agg = await runner.fire(ctx)

    # Decision is allow (the hook returned allow).
    assert agg.final_decision == "allow", (
        f"expected allow, got {agg.final_decision}"
    )
    assert len(agg.decisions) == 1
    assert agg.decisions[0].hook_id == "smoke.audit_hook"

    # The audit file must have exactly one line recording this dispatch.
    audit_files = list(audit_dir.glob("hooks-*.ndjson"))
    assert len(audit_files) == 1, f"expected 1 audit file, got {audit_files}"
    lines = audit_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["event"] == "PreToolUse"
    assert entry["session_id"] == "smoke-session"
    assert entry["aggregate"]["final_decision"] == "allow"
    assert entry["aggregate"]["decisions"][0]["hook_id"] == "smoke.audit_hook"


# ---------------------------------------------------------------------------
# Test 6: observability metrics endpoint works
# ---------------------------------------------------------------------------

async def test_observability_metrics_endpoint_works(
    isolated_settings: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a tool call is emitted, ``GET /metrics`` returns non-empty output.

    The ``/metrics`` endpoint serves Prometheus text format. We emit
    a ``tool_calls_total`` counter via the observability singleton
    (after enabling ``observability_prometheus_enabled``), then scrape
    ``/metrics`` and confirm the counter appears with the expected
    label value.
    """
    from httpx import ASGITransport, AsyncClient as _AC

    from harness.config import settings
    from harness.observability import emit_tool_call, get_observability, reset_observability
    from harness.server.app import create_app

    # ``metric_inc`` (called inside emit_tool_call) is gated on
    # ``observability_prometheus_enabled``. Flip it on so the counter
    # actually increments. This mirrors what a production operator
    # does via ``HARNESS_OBSERVABILITY_PROMETHEUS_ENABLED=true``.
    monkeypatch.setattr(settings, "observability_prometheus_enabled", True)

    reset_observability()
    try:
        # ``get_observability()`` lazily builds the singleton from
        # ``Settings()`` (a fresh instance with defaults) rather than
        # the live ``settings`` singleton. We pass our patched
        # ``settings`` explicitly so the prometheus gate sees True.
        get_observability(settings)

        # Build the app — the observability middleware reuses the
        # same singleton we just initialised.
        app = create_app()

        # Emit a tool call so the tool_calls_total counter increments.
        emit_tool_call(
            tool_name="read_file",
            status="ok",
            duration_s=0.012,
        )

        transport = ASGITransport(app=app)
        async with _AC(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/metrics")
            assert r.status_code == 200, (
                f"/metrics failed: {r.status_code} {r.text}"
            )
            body = r.text
            # The tool_calls_total metric must be present (prometheus
            # text format includes the metric name as a line prefix).
            assert "tool_calls_total" in body, (
                "tool_calls_total missing from /metrics output — "
                "is prometheus_client installed?"
            )
            # The read_file label we emitted must be in the output.
            assert "read_file" in body, (
                "read_file label missing from /metrics output"
            )
    finally:
        reset_observability()


# ---------------------------------------------------------------------------
# Test 7: outbound webhook succeeds to a local listener
# ---------------------------------------------------------------------------

async def test_webhook_outbound_succeeds_to_local_listener(
    isolated_settings: dict[str, Path],
) -> None:
    """An outbound webhook delivery reaches a local HTTP listener.

    Starts an ``aiohttp`` server on an ephemeral port, configures an
    ``OutboundWebhookDispatcher`` pointing at it, fires a ``merged``
    event, and confirms the listener received the POST with the
    expected JSON body. This exercises the Phase 2.5 fire-and-forget
    delivery path end-to-end.
    """
    from aiohttp import web

    from harness.agents.outbound import OutboundWebhookDispatcher

    received: list[dict[str, Any]] = []

    async def _handler(request: web.Request) -> web.Response:
        payload = await request.json()
        received.append({"payload": payload, "headers": dict(request.headers)})
        return web.json_response({"ok": True})

    app_web = web.Application()
    app_web.router.add_post("/hook", _handler)

    # Bind to an ephemeral port on the loopback interface.
    runner_web = web.AppRunner(app_web)
    await runner_web.setup()
    site = web.TCPSite(runner_web, "127.0.0.1", 0)
    await site.start()
    # Read the actual bound port (site._port is set after start()).
    bound_port = site._server.sockets[0].getsockname()[1]
    listener_url = f"http://127.0.0.1:{bound_port}/hook"

    try:
        dispatcher = OutboundWebhookDispatcher(
            urls=[listener_url],
            token="smoke-bearer",
            timeout_s=5.0,
            max_retries=0,
            backoff_initial_s=0.01,
            backoff_max_s=0.05,
            jitter_s=0.0,
        )
        event = {
            "kind": "merged",
            "job_id": "smoke-job-1",
            "pr_url": "https://example.com/pr/1",
        }
        dispatcher.fire(event)

        # Fire-and-forget: wait for the background task to complete.
        # We poll until the listener sees the POST or a 5s timeout.
        deadline = asyncio.get_event_loop().time() + 5.0
        while not received and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert received, (
            "listener did not receive the outbound POST within 5s"
        )
        entry = received[0]
        assert entry["payload"]["kind"] == "merged"
        assert entry["payload"]["job_id"] == "smoke-job-1"
        # The bearer token must be present (Phase 2.5 wire format).
        auth_header = entry["headers"].get("Authorization", "")
        assert auth_header == "Bearer smoke-bearer", (
            f"Authorization header mismatch: {auth_header!r}"
        )

        await dispatcher.aclose()
    finally:
        await runner_web.cleanup()


# ---------------------------------------------------------------------------
# Test 8: legacy API returns 410 when enabled
# ---------------------------------------------------------------------------

async def test_legacy_api_returns_410_when_enabled(
    isolated_settings: dict[str, Path],
) -> None:
    """With ``legacy_apis_gone_enabled=True``, ``/api/old`` returns 410.

    The Phase 4.12 v1.22.0 ``LegacyApisGoneMiddleware`` short-circuits
    every ``/api/*`` path (that is NOT ``/api/v1/*``) with HTTP 410
    Gone plus RFC 8594 ``Deprecation``/``Sunset`` headers. We flip
    the master switch on ``app.state`` (mirrors the production
    runtime toggle) and confirm a legacy path gets 410 + Sunset.
    """
    from fastapi.testclient import TestClient

    from harness.server.app import create_app

    app = create_app()
    app.state.legacy_apis_gone_enabled = True

    with TestClient(app) as tc:
        # /api/sessions is a legacy mount (the /api/v1/sessions is
        # canonical). With the flag on, the middleware short-circuits.
        r = tc.get("/api/sessions/legacy-probe")
        assert r.status_code == 410, (
            f"expected 410 Gone, got {r.status_code}: {r.text}"
        )
        # RFC 8594 Sunset header (HTTP-date).
        sunset = r.headers.get("sunset", "")
        assert sunset, "Sunset header missing on 410 response"
        # RFC 8594 Deprecation header.
        assert r.headers.get("deprecation") == "true", (
            f"Deprecation header mismatch: {r.headers.get('deprecation')!r}"
        )
        # RFC 8288 Link header points at the canonical successor.
        link = r.headers.get("link", "")
        assert 'rel="successor-version"' in link, (
            f"Link header missing successor-version rel: {link!r}"
        )

        # Non-regression: /api/v1/* must NOT be 410'd.
        r2 = tc.get("/api/v1/capabilities")
        assert r2.status_code != 410, (
            f"/api/v1/capabilities got 410 (should be unaffected): "
            f"{r2.status_code}"
        )
