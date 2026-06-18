"""Phase 4.11 v1.21.0: Tests for the SSE Elicitation transport.

Covers:
    1. ``GET /sse`` returns 403 when ``hooks_elicitation_sse_enabled`` is False.
    2. ``GET /sse`` returns a 200 ``text/event-stream`` when enabled.
    3. The stream emits ``event: new_question`` for a pending question.
    4. The stream emits ``: keep-alive`` comments on the heartbeat cadence.
    5. The stream emits ``event: answered`` when a question is resolved.
    6. The endpoint requires the ``elicitation.read`` scope (403 when the
       token lacks it, in enforced mode).
    7. A token WITH ``elicitation.read`` succeeds (200 streaming).
    8. The ``?session=`` filter isolates questions to the requested session.
    9. The ``seen_question_ids`` set deduplicates repeated ``new_question``
       emissions (the same pending question is announced exactly once).
    10. The generator exits when ``request.is_disconnected()`` returns True.
    11. The generator exits when ``max_session_age_s`` is exceeded.
    12. The SSE wire format matches the spec (``event:``/``data:``/blank line).

Testing strategy:
    The SSE endpoint produces an infinite stream, which the httpx
    ASGI transport cannot reliably cancel mid-flight (the transport
    keeps the event loop alive until the ASGI app returns). To work
    around this, every test sets a small ``max_age_s`` so the
    generator exits on its own shortly after the assertions run.
    The helper :func:`_drain_until_event` reads up to a fixed number
    of lines and returns as soon as the target event is seen — the
    generator then exits when ``max_age_s`` fires on the next poll.

    For tests that don't care about a specific event (e.g. the 200
    status check), we drain the whole stream and let ``max_age_s``
    terminate it.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from harness.elicitation import ElicitationBroker
from harness.server.auth.scopes import Scope
from harness.server.auth.tokens import TokenStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_broker() -> Any:
    """Reset the singleton broker before and after each test."""
    ElicitationBroker.reset()
    yield
    ElicitationBroker.reset()


async def _init_token_store(db_path) -> TokenStore:
    """Construct + init a TokenStore at the given path.

    The auth DB module caches an "initialised" flag at process level
    (see :func:`harness.server.auth.db._reset_init_flag`). Each test
    gets a fresh ``tmp_path``, so we reset the flag before init to
    force schema creation on the new path.
    """
    from harness.server.auth import db as auth_db

    auth_db._reset_init_flag()
    store = TokenStore(db_path)
    await store.init()
    return store


def _make_app(
    *,
    enabled: bool = True,
    heartbeat_s: float = 0.0,
    max_age_s: float = 2.0,
    auth_required: bool = False,
    token_store: TokenStore | None = None,
) -> FastAPI:
    """Build a minimal app with the SSE router wired up.

    ``heartbeat_s=0`` disables keep-alive (default for tests that
    don't exercise the heartbeat path).

    ``max_age_s=2.0`` is a safety net so every test's stream exits
    within ~2.5 seconds even if the test forgets to drain — the
    generator polls every 250ms so the worst-case exit latency is
    ~2.25s. Tests that explicitly verify max_age override this to
    a smaller value; tests that need a longer observation window
    override it to a larger value.
    """
    from harness.server.routes.elicitation_sse import router as sse_router

    app = FastAPI()
    app.include_router(sse_router, prefix="/api/v1/elicitation")
    app.state.hooks_elicitation_sse_enabled = enabled
    app.state.hooks_elicitation_sse_heartbeat_s = heartbeat_s
    app.state.hooks_elicitation_sse_max_session_age_s = max_age_s
    app.state.auth_required = auth_required
    # The scope dep always resolves ``get_token_store`` (FastAPI
    # dependency), even in open mode — so we must populate the store.
    app.state.token_store = token_store
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse_block(lines: list[str]) -> tuple[str | None, str | None]:
    """Parse a block of SSE lines into ``(event_name, data_json_str)``.

    A block looks like::

        event: new_question
        data: {...}

    Returns ``(None, None)`` if neither field is present.
    """
    event_name = None
    data_str = None
    for line in lines:
        if line.startswith("event: "):
            event_name = line[len("event: "):]
        elif line.startswith("data: "):
            data_str = line[len("data: "):]
    return event_name, data_str


async def _collect_events(
    resp,
    *,
    max_blocks: int = 20,
    target_events: set[str] | None = None,
    deadline: float | None = None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Collect SSE event blocks from a streaming response.

    Stops when either:
      - ``max_blocks`` event blocks have been parsed, or
      - all ``target_events`` have been seen (when ``target_events``
        is provided), or
      - ``deadline`` (monotonic seconds) has passed, or
      - the stream ends.

    Returns a list of ``(event_name, parsed_data_or_None)`` tuples.
    Heartbeat comments are skipped.

    Note: after this returns, the response stream may still be open
    on the server side. The caller is responsible for either (a)
    draining the rest, or (b) setting ``max_age_s`` small enough
    that the generator exits on its own. Without one of these,
    the ``async with ac.stream(...)`` block will hang on exit
    (httpx ASGI transport doesn't cancel the app coroutine).
    """
    if deadline is not None:
        deadline = time.monotonic() + deadline
    out: list[tuple[str, dict[str, Any] | None]] = []
    block: list[str] = []
    seen_targets: set[str] = set()
    async for line in resp.aiter_lines():
        if deadline is not None and time.monotonic() > deadline:
            break
        if line == "":
            # End of block — parse it.
            if block:
                name, data_str = _parse_sse_block(block)
                if name is not None:
                    data = json.loads(data_str) if data_str else None
                    out.append((name, data))
                    if target_events and name in target_events:
                        seen_targets.add(name)
                        if seen_targets == target_events:
                            break
                block = []
            continue
        if line.startswith(":"):
            # Comment / heartbeat — skip.
            continue
        block.append(line)
        if len(out) >= max_blocks:
            break
    return out


async def _drain_remaining(resp, *, timeout_s: float = 8.0) -> None:
    """Drain remaining lines until the stream ends or timeout.

    Use AFTER the test's primary assertions are done — this lets the
    server generator exit (via ``max_age_s``) so the ``async with``
    block doesn't hang on exit. Safe to call even if the stream was
    partially consumed (it catches StreamConsumed).
    """
    deadline = time.monotonic() + timeout_s
    try:
        async for _ in resp.aiter_lines():
            if time.monotonic() > deadline:
                break
    except Exception:  # noqa: BLE001 — StreamConsumed or similar
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestElicitationSSE:
    """All 12 spec'd v1.21.0 tests."""

    async def test_sse_endpoint_disabled_returns_403(
        self, tmp_path,
    ) -> None:
        """hooks_elicitation_sse_enabled=False → 403.

        No stream is opened (the handler raises before returning the
        StreamingResponse), so the response is a normal JSON error.
        """
        store = await _init_token_store(tmp_path / "auth.db")
        app = _make_app(enabled=False, token_store=store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get("/api/v1/elicitation/sse")
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "sse_disabled"

    async def test_sse_endpoint_enabled_returns_streaming_response(
        self, tmp_path,
    ) -> None:
        """Enabled → 200 + ``text/event-stream`` media type.

        We set a tiny ``max_age_s`` so the generator exits on its own
        after the first poll iteration — otherwise the infinite
        stream would hang the test.
        """
        store = await _init_token_store(tmp_path / "auth.db")
        app = _make_app(enabled=True, max_age_s=0.3, token_store=store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream("GET", "/api/v1/elicitation/sse") as resp:
                assert resp.status_code == 200
                ct = resp.headers.get("content-type", "")
                assert "text/event-stream" in ct, ct
                # Drain to allow the generator to exit cleanly.
                await _drain_remaining(resp, timeout_s=2.0)

    async def test_sse_streams_new_question_event(
        self, tmp_path,
    ) -> None:
        """A pending question → ``event: new_question`` with payload."""
        store = await _init_token_store(tmp_path / "auth.db")
        app = _make_app(enabled=True, token_store=store)
        broker = ElicitationBroker.get()
        qid = broker.publish(
            question="Run rm -rf /tmp/foo?",
            options=["proceed", "abort"],
            default_answer="abort",
            session_id="sess-A",
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream("GET", "/api/v1/elicitation/sse") as resp:
                events = await _collect_events(
                    resp, target_events={"new_question"}, deadline=3.0,
                )
                await _drain_remaining(resp, timeout_s=6.0)
        new_q = [d for n, d in events if n == "new_question"]
        assert len(new_q) == 1, f"expected 1 new_question, got {len(new_q)}"
        data = new_q[0]
        assert data["question_id"] == qid
        assert data["question"] == "Run rm -rf /tmp/foo?"
        assert data["options"] == ["proceed", "abort"]
        assert data["default_answer"] == "abort"
        assert data["session_id"] == "sess-A"

    async def test_sse_streams_heartbeat(self, tmp_path) -> None:
        """``: keep-alive`` is emitted on the heartbeat cadence."""
        store = await _init_token_store(tmp_path / "auth.db")
        # heartbeat=0.1s so the test doesn't wait the full 15s default.
        app = _make_app(
            enabled=True, heartbeat_s=0.1, max_age_s=2.0, token_store=store,
        )
        transport = ASGITransport(app=app)
        got_heartbeat = False
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream("GET", "/api/v1/elicitation/sse") as resp:
                # Read raw lines to catch the comment (which
                # ``_collect_events`` skips).
                deadline = time.monotonic() + 3.0
                async for line in resp.aiter_lines():
                    if "keep-alive" in line:
                        got_heartbeat = True
                        break
                    if time.monotonic() > deadline:
                        break
                await _drain_remaining(resp, timeout_s=4.0)
        assert got_heartbeat, "no keep-alive comment received"

    async def test_sse_streams_answered_event(
        self, tmp_path,
    ) -> None:
        """Resolving a question → ``event: answered`` (or ``timeout``).

        Without the decision store attached, the SSE route classifies
        the resolution as ``answered`` (the default fallback). This
        test verifies that SOME resolution event fires after the
        question leaves ``pending()`` and that the payload carries
        the question_id.

        Strategy:
            1. Publish a question (lands in ``_pending``).
            2. Open the SSE stream — generator emits ``new_question``.
            3. Start a background ``broker.wait(qid)`` task — this is
               the consumer that will pop the question from
               ``_pending`` once the future resolves.
            4. Call ``broker.answer()`` from another background task.
               The ``wait()`` task's future resolves, ``wait()``
               returns, and its ``finally`` block removes the
               question from ``_pending``.
            5. The SSE generator's next poll sees the question gone
               and emits the resolution event.
        """
        store = await _init_token_store(tmp_path / "auth.db")
        # heartbeat=0.3s so the stream keeps emitting events and
        # httpx's iterator keeps yielding — without heartbeats the
        # iterator may stall after the new_question block while the
        # SSE generator sleeps on its 250ms poll. A short heartbeat
        # keeps the iterator "waking up" so the answered event is
        # delivered as soon as it's emitted.
        app = _make_app(
            enabled=True, heartbeat_s=0.3, max_age_s=10.0, token_store=store,
        )
        broker = ElicitationBroker.get()
        qid = broker.publish(
            question="Approve?",
            default_answer="no",
            timeout_s=30.0,
            session_id="sess-B",
        )
        transport = ASGITransport(app=app)
        saw_new_question = False
        resolved_data: dict[str, Any] | None = None

        # Start the waiter + answerer BEFORE opening the stream. The
        # ASGI test transport seems to pin the event loop such that
        # tasks created inside the ``async with ac.stream(...)`` block
        # don't get scheduled until the stream iterator yields. Since
        # the iterator only yields when the server emits a line, and
        # the server is waiting for the question to be answered, we
        # get a deadlock. Starting the tasks first breaks the cycle.
        async def _waiter() -> str:
            return await broker.wait(qid)

        waiter_task = asyncio.create_task(_waiter())

        async def _answer() -> None:
            # 0.5s gives the SSE generator ~2 poll iterations to
            # emit ``new_question`` before the answer lands.
            await asyncio.sleep(0.5)
            broker.answer(qid, "yes", source="sse")

        answer_task = asyncio.create_task(_answer())

        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream("GET", "/api/v1/elicitation/sse") as resp:
                # Single sweep — parse blocks as they arrive.
                deadline = time.monotonic() + 12.0
                block: list[str] = []
                async for line in resp.aiter_lines():
                    if time.monotonic() > deadline:
                        break
                    if line == "":
                        if block:
                            name, data_str = _parse_sse_block(block)
                            if name == "new_question":
                                saw_new_question = True
                            elif name in ("answered", "timeout") and data_str:
                                resolved_data = json.loads(data_str)
                                break
                        block = []
                        continue
                    if line.startswith(":"):
                        continue
                    block.append(line)
        # Reap the background tasks.
        for t in (waiter_task, answer_task):
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()
        assert saw_new_question, "no new_question event received"
        assert resolved_data is not None, "no resolution event received"
        assert resolved_data["question_id"] == qid
    async def test_sse_requires_elicitation_read_scope(
        self, tmp_path,
    ) -> None:
        """A token without ``elicitation.read`` → 403 (auth enforced).

        No stream is opened (the scope dep raises before the handler
        body runs), so this is a normal JSON response.
        """
        store = await _init_token_store(tmp_path / "auth.db")
        plaintext, _ = await store.create("wrong", {Scope.AGENTS_READ})
        app = _make_app(
            enabled=True, auth_required=True, token_store=store,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.get(
                "/api/v1/elicitation/sse",
                headers={"Authorization": f"Bearer {plaintext}"},
            )
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert "missing required scope" in detail
        assert "elicitation.read" in detail

    async def test_sse_token_with_elicitation_read_succeeds(
        self, tmp_path,
    ) -> None:
        """A token WITH ``elicitation.read`` → 200 streaming."""
        store = await _init_token_store(tmp_path / "auth.db")
        plaintext, _ = await store.create("ok", {Scope.ELICITATION_READ})
        app = _make_app(
            enabled=True, auth_required=True, max_age_s=0.3, token_store=store,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream(
                "GET",
                "/api/v1/elicitation/sse",
                headers={"Authorization": f"Bearer {plaintext}"},
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get(
                    "content-type", "",
                )
                await _drain_remaining(resp, timeout_s=2.0)

    async def test_sse_session_filter_isolates_questions(
        self, tmp_path,
    ) -> None:
        """``?session=S`` filters out questions with other session_ids."""
        store = await _init_token_store(tmp_path / "auth.db")
        app = _make_app(enabled=True, token_store=store)
        broker = ElicitationBroker.get()
        qid_a = broker.publish(
            question="for A", default_answer="a", session_id="sess-A",
        )
        qid_b = broker.publish(
            question="for B", default_answer="b", session_id="sess-B",
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream(
                "GET", "/api/v1/elicitation/sse?session=sess-A",
            ) as resp:
                events = await _collect_events(
                    resp, deadline=2.0, max_blocks=10,
                )
                await _drain_remaining(resp, timeout_s=6.0)
        new_qs = [d for n, d in events if n == "new_question"]
        # Only sess-A's question should appear.
        assert len(new_qs) == 1, (
            f"expected 1 new_question for sess-A, got {len(new_qs)}: {new_qs}"
        )
        assert new_qs[0]["question_id"] == qid_a
        assert new_qs[0]["session_id"] == "sess-A"
        # sess-B's question must NOT be in the stream.
        assert all(d["question_id"] != qid_b for d in new_qs), (
            "sess-B question leaked through session filter"
        )

    async def test_sse_seen_questions_deduplicated(
        self, tmp_path,
    ) -> None:
        """The same pending question is announced exactly once.

        Without dedup the same question would fire ``new_question``
        on every 250ms poll iteration.
        """
        store = await _init_token_store(tmp_path / "auth.db")
        app = _make_app(enabled=True, max_age_s=2.0, token_store=store)
        broker = ElicitationBroker.get()
        broker.publish(
            question="dedup me", default_answer="abort",
        )
        transport = ASGITransport(app=app)
        new_question_count = 0
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream("GET", "/api/v1/elicitation/sse") as resp:
                # Read for ~1s — enough for ~4 poll iterations at 250ms.
                # If dedup is broken we'd see ~4 new_question events.
                deadline = time.monotonic() + 1.0
                async for line in resp.aiter_lines():
                    if "event: new_question" in line:
                        new_question_count += 1
                    if time.monotonic() > deadline:
                        break
                await _drain_remaining(resp, timeout_s=4.0)
        assert new_question_count == 1, (
            f"expected exactly 1 new_question, got {new_question_count}"
        )

    async def test_sse_client_disconnect_breaks_stream(
        self, tmp_path,
    ) -> None:
        """``request.is_disconnected()`` → True breaks the generator.

        We mock ``is_disconnected`` to return True after the first
        poll — the generator should exit cleanly on the next
        iteration. The ASGI test transport doesn't surface real
        client disconnects to the request object, so the mock is
        necessary.
        """
        store = await _init_token_store(tmp_path / "auth.db")
        app = _make_app(enabled=True, token_store=store)
        transport = ASGITransport(app=app)
        call_count = {"n": 0}

        async def _fake_disconnected(self) -> bool:
            call_count["n"] += 1
            # Return True on the 2nd call so at least one poll
            # iteration runs (proving the generator started).
            return call_count["n"] >= 2

        from starlette.requests import Request as StarletteRequest

        with patch.object(
            StarletteRequest, "is_disconnected", _fake_disconnected,
        ):
            async with AsyncClient(
                transport=transport, base_url="http://t",
            ) as ac:
                async with ac.stream(
                    "GET", "/api/v1/elicitation/sse",
                ) as resp:
                    # The generator should exit quickly after the mock
                    # fires True. Drain with a short timeout — if the
                    # mock didn't work, the stream would hang until
                    # max_age_s (5s default).
                    await _drain_remaining(resp, timeout_s=3.0)
        # ``is_disconnected`` must have been called at least twice
        # (once where it returned False, once where it returned True).
        assert call_count["n"] >= 2, (
            f"is_disconnected called {call_count['n']} times (expected >=2)"
        )

    async def test_sse_max_session_age_disconnects(
        self, tmp_path,
    ) -> None:
        """``max_session_age_s`` exceeded → generator exits."""
        store = await _init_token_store(tmp_path / "auth.db")
        # max_age=0.3s so the test doesn't wait the full hour default.
        app = _make_app(
            enabled=True, max_age_s=0.3, token_store=store,
        )
        transport = ASGITransport(app=app)
        start = time.monotonic()
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream("GET", "/api/v1/elicitation/sse") as resp:
                # Read until the stream ends (generator return).
                async for _ in resp.aiter_lines():
                    pass
        elapsed = time.monotonic() - start
        # The generator should have exited shortly after max_age (0.3s).
        # Allow generous fudge for scheduling overhead on slow CI runners.
        assert elapsed < 2.0, (
            f"stream ran for {elapsed:.2f}s (expected <2.0s with max_age=0.3s)"
        )

    async def test_sse_format_correct(self, tmp_path) -> None:
        """The wire format matches the SSE spec.

        Each event block is exactly::

            event: <name>\n
            data: <json>\n
            \n

        We verify the byte-level structure on a ``new_question`` event.
        """
        store = await _init_token_store(tmp_path / "auth.db")
        app = _make_app(enabled=True, token_store=store)
        broker = ElicitationBroker.get()
        broker.publish(
            question="format check", default_answer="abort",
            session_id="sess-F",
        )
        transport = ASGITransport(app=app)
        first_chunk = b""
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            async with ac.stream("GET", "/api/v1/elicitation/sse") as resp:
                # The first non-empty byte chunk should be a full SSE
                # block (the generator yields the new_question event
                # before the first poll sleep).
                deadline = time.monotonic() + 3.0
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        first_chunk = chunk
                        break
                    if time.monotonic() > deadline:
                        break
                await _drain_remaining(resp, timeout_s=6.0)
        text = first_chunk.decode("utf-8")
        # Must start with ``event: new_question\n``.
        assert text.startswith("event: new_question\n"), repr(text[:50])
        # Must contain ``data: `` followed by valid JSON.
        assert "data: " in text
        # Must end with the block terminator ``\n\n``.
        assert text.endswith("\n\n"), repr(text[-10:])
        # The data payload must parse as JSON.
        data_line = [
            line for line in text.split("\n") if line.startswith("data: ")
        ][0]
        payload = json.loads(data_line[len("data: "):])
        assert "question_id" in payload
        assert payload["question"] == "format check"
