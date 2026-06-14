"""Tests for ``harness.agents.outbound.OutboundWebhookDispatcher`` (Phase 2.5)."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from harness.agents.outbound import (
    OUTBOUND_EVENT_KINDS,
    OutboundWebhookDispatcher,
    parse_urls,
)


# === parse_urls ===

class TestParseUrls:
    def test_empty_string_returns_empty(self) -> None:
        assert parse_urls("") == []

    def test_single_url(self) -> None:
        assert parse_urls("http://localhost:9000/hook") == [
            "http://localhost:9000/hook",
        ]

    def test_multiple_urls_comma_separated(self) -> None:
        assert parse_urls(
            "http://a,http://b,http://c",
        ) == ["http://a", "http://b", "http://c"]

    def test_trims_whitespace(self) -> None:
        assert parse_urls(
            " http://a , http://b ,http://c ",
        ) == ["http://a", "http://b", "http://c"]

    def test_drops_empty_entries(self) -> None:
        # Trailing comma, double commas, etc.
        assert parse_urls(
            "http://a,,http://b,,",
        ) == ["http://a", "http://b"]


# === should_fire ===

class TestShouldFire:
    def test_forwarded_kinds(self) -> None:
        d = OutboundWebhookDispatcher(urls=())
        for k in OUTBOUND_EVENT_KINDS:
            assert d.should_fire(k), f"expected {k} to fire"

    def test_non_forwarded_kinds_dropped(self) -> None:
        d = OutboundWebhookDispatcher(urls=())
        for k in ("pr_creating", "running_code", "code_done", "pr_open"):
            assert not d.should_fire(k), f"{k} should not fire"

    def test_unknown_kind_dropped(self) -> None:
        d = OutboundWebhookDispatcher(urls=())
        assert not d.should_fire("totally_made_up")
        assert not d.should_fire("")


# === fire (filter + scheduling) ===

class TestFireScheduling:
    def test_fire_drops_unknown_kind(self) -> None:
        d = OutboundWebhookDispatcher(urls=("http://x",))
        # No task should be scheduled for a non-forwarded kind.
        d.fire({"kind": "pr_creating", "job_id": "j1"})
        # We can't easily assert "no task scheduled" but we can
        # assert that aclose() is clean.

    def test_fire_no_op_when_urls_empty(self) -> None:
        d = OutboundWebhookDispatcher(urls=())
        d.fire({"kind": "merged", "job_id": "j1"})
        # No exception, no task scheduled. Behavior: silent skip.

    def test_fire_drops_when_no_event_loop(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If called from sync code with no running loop, the
        # dispatcher should drop + log a warning (not raise).
        monkeypatch.setattr(
            "asyncio.get_running_loop",
            lambda: (_ for _ in ()).throw(RuntimeError()),
        )
        d = OutboundWebhookDispatcher(urls=("http://x",))
        d.fire({"kind": "merged", "job_id": "j1"})
        # No exception, just a logged warning.


# === _deliver_one (fake transport) ===

class _FakeTransport(httpx.AsyncBaseTransport):
    """In-memory httpx transport for tests.

    Records every request and returns a pre-programmed response
    (or sequence of responses) for each call. No network I/O.
    """

    def __init__(
        self, responses: list[httpx.Response | Exception],
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        self._idx = 0

    async def handle_async_request(
        self, request: httpx.Request,
    ) -> httpx.Response:
        self.requests.append(request)
        if self._idx >= len(self._responses):
            # Default: 200 OK (caller probably forgot to queue
            # enough responses; we make the test fail naturally
            # via "more requests than responses" assertions).
            return httpx.Response(200, request=request)
        item = self._responses[self._idx]
        self._idx += 1
        if isinstance(item, Exception):
            raise item
        return item


def _client_with(transport: _FakeTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport, timeout=5.0,
    )


class TestDelivery:
    @pytest.mark.asyncio
    async def test_happy_path_2xx(self) -> None:
        transport = _FakeTransport([
            httpx.Response(200, request=None),
        ])
        client = _client_with(transport)
        d = OutboundWebhookDispatcher(
            urls=("http://h/hook",), token="tok",
            http_client=client, max_retries=0,
        )
        await d._deliver({"kind": "merged", "job_id": "j1"})
        assert len(transport.requests) == 1
        # Authorization header was sent.
        auth = transport.requests[0].headers.get("authorization")
        assert auth == "Bearer tok"
        # Body was the event.
        body = transport.requests[0].read()
        import json
        assert json.loads(body) == {"kind": "merged", "job_id": "j1"}

    @pytest.mark.asyncio
    async def test_4xx_no_retry(self) -> None:
        transport = _FakeTransport([
            httpx.Response(404, text="not found"),
        ])
        client = _client_with(transport)
        d = OutboundWebhookDispatcher(
            urls=("http://h/hook",),
            http_client=client, max_retries=3,
        )
        await d._deliver({"kind": "merged", "job_id": "j1"})
        # Only one request — 4xx doesn't retry.
        assert len(transport.requests) == 1

    @pytest.mark.asyncio
    async def test_5xx_retries_then_succeeds(self) -> None:
        transport = _FakeTransport([
            httpx.Response(500, text="boom"),
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, request=None),
        ])
        client = _client_with(transport)
        d = OutboundWebhookDispatcher(
            urls=("http://h/hook",),
            http_client=client,
            max_retries=3,
            backoff_initial_s=0.0,  # tests run fast
            jitter_s=0.0,
        )
        await d._deliver({"kind": "merged", "job_id": "j1"})
        assert len(transport.requests) == 3

    @pytest.mark.asyncio
    async def test_5xx_exhausts_retries(self) -> None:
        transport = _FakeTransport(
            [httpx.Response(500, text="x")] * 10,  # more than enough
        )
        client = _client_with(transport)
        d = OutboundWebhookDispatcher(
            urls=("http://h/hook",),
            http_client=client,
            max_retries=2,
            backoff_initial_s=0.0, jitter_s=0.0,
        )
        # Should NOT raise — exhausted retries log + drop.
        await d._deliver({"kind": "merged", "job_id": "j1"})
        # 1 initial + 2 retries = 3 attempts.
        assert len(transport.requests) == 3

    @pytest.mark.asyncio
    async def test_timeout_treated_as_retryable(self) -> None:
        transport = _FakeTransport([
            httpx.TimeoutException("too slow"),
            httpx.Response(200, request=None),
        ])
        client = _client_with(transport)
        d = OutboundWebhookDispatcher(
            urls=("http://h/hook",),
            http_client=client,
            max_retries=3,
            backoff_initial_s=0.0, jitter_s=0.0,
        )
        await d._deliver({"kind": "merged", "job_id": "j1"})
        assert len(transport.requests) == 2

    @pytest.mark.asyncio
    async def test_no_token_no_authorization_header(self) -> None:
        transport = _FakeTransport([httpx.Response(200, request=None)])
        client = _client_with(transport)
        d = OutboundWebhookDispatcher(
            urls=("http://h/hook",), token="",
            http_client=client, max_retries=0,
        )
        await d._deliver({"kind": "merged", "job_id": "j1"})
        assert "authorization" not in transport.requests[0].headers

    @pytest.mark.asyncio
    async def test_multiple_urls_concurrent(self) -> None:
        transport = _FakeTransport([
            httpx.Response(200, request=None),
            httpx.Response(200, request=None),
        ])
        client = _client_with(transport)
        d = OutboundWebhookDispatcher(
            urls=("http://a/hook", "http://b/hook"),
            http_client=client, max_retries=0,
        )
        await d._deliver({"kind": "merged", "job_id": "j1"})
        assert len(transport.requests) == 2


# === aclose ===

class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_closes_owned_client(self) -> None:
        d = OutboundWebhookDispatcher(urls=("http://h/hook",))
        await d.aclose()
        assert d._client.is_closed

    @pytest.mark.asyncio
    async def test_aclose_leaves_injected_client_alone(self) -> None:
        injected = httpx.AsyncClient(timeout=5.0)
        d = OutboundWebhookDispatcher(
            urls=("http://h/hook",), http_client=injected,
        )
        await d.aclose()
        assert not injected.is_closed
        await injected.aclose()
