"""Phase 4.3+ v1.15.0: Tests for HTTP long-poll Elicitation fallback.

Covers:
    1. GET /poll returns a pending question immediately (200).
    2. GET /poll returns 404 when no question is pending (timeout).
    3. POST /answer resolves the broker future.
    4. Long-poll endpoints return 403 when
       ``hooks_elicitation_longpoll_enabled`` is False.
    5. Long-poll with a short timeout returns 404 (timeout handling).

The tests build a minimal FastAPI app that mounts only the long-poll
router — the full ``create_app()`` lifespan is too heavy for these
unit tests (it spins up the LLM router, JobStore, compactor, etc.).
The ``app.state.hooks_elicitation_longpoll_enabled`` /
``_timeout_s`` / ``_interval_s`` flags are set explicitly so the route
doesn't have to construct ``Settings()`` (matches the WS test style
in ``test_elicitation_broker.py``).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient

from harness.elicitation import ElicitationBroker


@pytest.fixture(autouse=True)
def reset_broker() -> Any:
    """Reset the singleton broker before and after each test."""
    ElicitationBroker.reset()
    yield
    ElicitationBroker.reset()


def _make_app(
    *,
    enabled: bool = True,
    timeout_s: float = 30.0,
    interval_s: float = 0.05,
) -> FastAPI:
    """Build a minimal app with the long-poll router wired up.

    Mirrors the mount in ``harness/server/app.py``: same prefix,
    same ``app.state`` flags. Using a small ``interval_s`` keeps the
    tests fast (the broker has no question → the route polls every
    50ms instead of 250ms).
    """
    from harness.server.routes.elicitation_longpoll import (
        router as elicitation_longpoll_router,
    )

    app = FastAPI()
    app.include_router(
        elicitation_longpoll_router, prefix="/api/v1/elicitation",
    )
    app.state.hooks_elicitation_longpoll_enabled = enabled
    app.state.hooks_elicitation_longpoll_timeout_s = timeout_s
    app.state.hooks_elicitation_longpoll_interval_s = interval_s

    # v1.0.0 fix: /poll needs elicitation.read, /answer needs
    # elicitation.write. Inject a permissive fake store so the legacy
    # tests can keep working without per-test token plumbing.
    from harness.server.auth.scopes import Scope

    class _FakeRecord:
        is_active = True
        scopes = frozenset({Scope.ELICITATION_READ, Scope.ELICITATION_WRITE})

    class _FakeStore:
        async def lookup(self, plaintext: str) -> _FakeRecord | None:
            return _FakeRecord() if plaintext else None

    app.state.token_store = _FakeStore()
    return app


class TestElicitationLongPoll:
    """All 5 spec'd v1.15.0 tests."""

    def test_longpoll_returns_pending_question(self) -> None:
        """Publish a question, GET /poll, expect 200 with the question."""
        client = TestClient(_make_app(timeout_s=2.0)); client.headers.update({"Authorization": "Bearer x"})
        broker = ElicitationBroker.get()
        qid = broker.publish(
            question="Run rm -rf /tmp/foo?",
            options=["proceed", "abort"],
            default_answer="abort",
        )

        resp = client.get("/api/v1/elicitation/poll")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["question_id"] == qid
        assert data["question"] == "Run rm -rf /tmp/foo?"
        assert data["options"] == ["proceed", "abort"]
        assert data["default_answer"] == "abort"

    def test_longpoll_returns_404_when_no_question(self) -> None:
        """No pending question → 404 with detail=no_pending_question."""
        client = TestClient(_make_app(timeout_s=0.1, interval_s=0.02)); client.headers.update({"Authorization": "Bearer x"})
        resp = client.get("/api/v1/elicitation/poll")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["detail"] == "no_pending_question"

    @pytest.mark.asyncio
    async def test_longpoll_resolves_on_answer_post(self) -> None:
        """POST /answer → broker.wait() future resolves with the value."""
        client = TestClient(_make_app(timeout_s=2.0)); client.headers.update({"Authorization": "Bearer x"})
        broker = ElicitationBroker.get()
        qid = broker.publish(
            question="Approve deployment?",
            default_answer="no",
            timeout_s=5.0,
        )

        async def waiter() -> str:
            return await broker.wait(qid)

        waiter_task = asyncio.create_task(waiter())
        # Yield so the waiter reaches the future-creation point.
        await asyncio.sleep(0.05)

        resp = client.post(
            "/api/v1/elicitation/answer",
            json={
                "session_id": "test-session",
                "question_id": qid,
                "answer": "yes",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True
        assert body["question_id"] == qid

        result = await waiter_task
        assert result == "yes"

    def test_longpoll_disabled_when_setting_false(self) -> None:
        """hooks_elicitation_longpoll_enabled=False → 403 on both endpoints."""
        client = TestClient(_make_app(enabled=False)); client.headers.update({"Authorization": "Bearer x"})

        resp_get = client.get("/api/v1/elicitation/poll")
        assert resp_get.status_code == 403, resp_get.text
        assert resp_get.json()["detail"] == "longpoll_disabled"

        resp_post = client.post(
            "/api/v1/elicitation/answer",
            json={"question_id": "any", "answer": "x"},
        )
        assert resp_post.status_code == 403, resp_post.text
        assert resp_post.json()["detail"] == "longpoll_disabled"

    def test_longpoll_timeout_returns_empty(self) -> None:
        """No question within the (short) timeout → 404 / empty.

        Uses a 1-second timeout as suggested in the spec — fast
        enough for a test, slow enough to exercise the polling loop
        at least a few times.
        """
        client = TestClient(
            _make_app(timeout_s=1.0, interval_s=0.05),
        )
        client.headers.update({"Authorization": "Bearer x"})
        # No publish — nothing to poll for.
        import time as _time

        t0 = _time.monotonic()
        resp = client.get("/api/v1/elicitation/poll")
        elapsed = _time.monotonic() - t0

        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "no_pending_question"
        # The endpoint should have blocked for ~1s (not returned
        # instantly). Allow a small fudge for scheduling overhead.
        assert elapsed >= 0.9, (
            f"long-poll returned too fast: {elapsed:.2f}s "
            f"(expected ~1.0s)"
        )
