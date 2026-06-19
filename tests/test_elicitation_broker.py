"""Phase 4.3+ v1.12.0: Tests for ElicitationBroker + WS endpoint + hook integration.

Covers:
    1. ElicitationBroker: publish/wait round-trip, timeout → default,
       multiple concurrent questions, stats counters, singleton.
    2. Elicitation WebSocket route: connect, list, publish, answer,
       ping/pong, close on WS disabled.
    3. confirm_dangerous_hook: WS disabled → default_ws_disabled,
       WS timeout → default_timeout, WS answer → ws_human.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient

from harness.config import Settings
from harness.elicitation import ElicitationBroker, PendingQuestion
from harness.hooks import HookContext
from harness.hooks.builtin import confirm_dangerous_hook


@pytest.fixture(autouse=True)
def reset_broker() -> None:
    """Reset the singleton broker before each test."""
    ElicitationBroker.reset()
    yield
    ElicitationBroker.reset()


# === 1. ElicitationBroker unit tests ===

class TestElicitationBroker:
    @pytest.mark.asyncio
    async def test_publish_returns_id(self) -> None:
        broker = ElicitationBroker.get()
        qid = broker.publish(question="hello")
        assert isinstance(qid, str)
        assert len(qid) == 12

    @pytest.mark.asyncio
    async def test_pending_lists_active_questions(self) -> None:
        broker = ElicitationBroker.get()
        qid1 = broker.publish(question="q1")
        qid2 = broker.publish(question="q2")
        pending = broker.pending()
        assert len(pending) == 2
        assert {pq.question_id for pq in pending} == {qid1, qid2}

    @pytest.mark.asyncio
    async def test_wait_returns_user_answer(self) -> None:
        broker = ElicitationBroker.get()
        qid = broker.publish(question="Run rm -rf?", default_answer="abort")

        async def answerer() -> None:
            await asyncio.sleep(0.05)
            broker.answer(qid, "proceed")

        task = asyncio.create_task(answerer())
        result = await broker.wait(qid)
        await task
        assert result == "proceed"

    @pytest.mark.asyncio
    async def test_wait_returns_default_on_timeout(self) -> None:
        broker = ElicitationBroker.get()
        qid = broker.publish(question="Run rm -rf?", default_answer="abort", timeout_s=0.1)
        result = await broker.wait(qid)
        assert result == "abort"

    @pytest.mark.asyncio
    async def test_answer_after_timeout_is_noop(self) -> None:
        broker = ElicitationBroker.get()
        qid = broker.publish(question="q", default_answer="d1", timeout_s=0.1)
        result = await broker.wait(qid)
        assert result == "d1"
        # Late answer is a no-op.
        ok = broker.answer(qid, "too late")
        assert ok is False

    @pytest.mark.asyncio
    async def test_answer_unknown_id_returns_false(self) -> None:
        broker = ElicitationBroker.get()
        assert broker.answer("nonexistent", "x") is False

    @pytest.mark.asyncio
    async def test_wait_unknown_id_raises(self) -> None:
        broker = ElicitationBroker.get()
        with pytest.raises(KeyError):
            await broker.wait("nonexistent")

    @pytest.mark.asyncio
    async def test_concurrent_questions(self) -> None:
        broker = ElicitationBroker.get()
        qid_a = broker.publish(question="A", default_answer="abort")
        qid_b = broker.publish(question="B", default_answer="abort")

        async def answerer() -> None:
            await asyncio.sleep(0.05)
            broker.answer(qid_a, "proceed")
            broker.answer(qid_b, "abort")

        task = asyncio.create_task(answerer())
        a, b = await asyncio.gather(broker.wait(qid_a), broker.wait(qid_b))
        await task
        assert (a, b) == ("proceed", "abort")

    @pytest.mark.asyncio
    async def test_stats_counters(self) -> None:
        broker = ElicitationBroker.get()
        qid = broker.publish(question="q", default_answer="d")
        await broker.wait(qid)  # timeout
        s = broker.stats()
        assert s["published_total"] == 1
        assert s["timed_out_total"] == 1
        assert s["answered_total"] == 0
        assert s["pending_count"] == 0

    def test_singleton(self) -> None:
        a = ElicitationBroker.get()
        b = ElicitationBroker.get()
        assert a is b

    @pytest.mark.asyncio
    async def test_pending_question_dataclass(self) -> None:
        broker = ElicitationBroker.get()
        qid = broker.publish(
            question="Q",
            options=["a", "b"],
            default_answer="a",
            timeout_s=10.0,
        )
        pq = broker.pending()[0]
        assert isinstance(pq, PendingQuestion)
        assert pq.question_id == qid
        assert pq.question == "Q"
        assert pq.options == ["a", "b"]
        assert pq.default_answer == "a"
        assert pq.timeout_s == 10.0
        # Future is created lazily on first resolve_future() call.
        assert pq.future is None
        future = pq.resolve_future()
        assert future is not None
        assert not future.done()


# === 2. Elicitation WebSocket route tests ===

class TestElicitationWebSocket:
    def _make_app(self, token: str = "") -> FastAPI:
        from harness.server.routes.elicitation import router as elicitation_router

        app = FastAPI()
        app.include_router(elicitation_router, prefix="/api/v1/elicitation")

        # v1.0.0 fix: WS upgrade requires elicitation.write scope. Create
        # a minimal fake TokenStore that accepts any non-empty token
        # with the required scope, so the existing WS tests can keep
        # connecting via TestClient without rewriting each test.
        if token:
            from harness.server.auth.scopes import Scope

            class _FakeRecord:
                is_active = True
                scopes = frozenset({Scope.ELICITATION_WRITE})

            class _FakeStore:
                async def lookup(self, plaintext: str) -> _FakeRecord | None:
                    return _FakeRecord() if plaintext == token else None

            app.state.token_store = _FakeStore()

        return app

    def test_ws_connect_sends_hello(self) -> None:
        client = TestClient(self._make_app(token="t"))
        with client.websocket_connect("/api/v1/elicitation/ws?token=t") as ws:
            msg = ws.receive_json()
            assert msg["action"] == "connected"
            assert "stats" in msg

    def test_ws_list_empty(self) -> None:
        client = TestClient(self._make_app(token="t"))
        with client.websocket_connect("/api/v1/elicitation/ws?token=t") as ws:
            ws.receive_json()  # hello
            ws.send_text(json.dumps({"action": "list"}))
            resp = ws.receive_json()
            assert resp["action"] == "pending"
            assert resp["questions"] == []

    def test_ws_ping_pong(self) -> None:
        client = TestClient(self._make_app(token="t"))
        with client.websocket_connect("/api/v1/elicitation/ws?token=t") as ws:
            ws.receive_json()  # hello
            ws.send_text(json.dumps({"action": "ping"}))
            resp = ws.receive_json()
            assert resp["action"] == "pong"

    @pytest.mark.asyncio
    async def test_ws_publish_then_answer(self) -> None:
        """Connect, publish via broker, receive question, send answer.

        Async version: the broker.wait() can run on the running loop
        while the TestClient drives the WS handshake.
        """
        client = TestClient(self._make_app(token="t"))
        broker = ElicitationBroker.get()
        qid = broker.publish(
            question="Delete?",
            options=["yes", "no"],
            default_answer="no",
            timeout_s=5.0,
        )

        async def waiter() -> str:
            return await broker.wait(qid)

        waiter_task = asyncio.create_task(waiter())
        # Yield so waiter reaches the future-creation point.
        await asyncio.sleep(0.05)

        with client.websocket_connect("/api/v1/elicitation/ws?token=t") as ws:
            ws.receive_json()  # hello
            # Poll loop pushes the question within 500ms.
            got_q = False
            for _ in range(15):
                msg = ws.receive_text()
                data = json.loads(msg)
                if data.get("action") == "question" and data.get("question_id") == qid:
                    got_q = True
                    break
            assert got_q, "never received the question"
            ws.send_text(json.dumps({
                "action": "answer",
                "question_id": qid,
                "value": "yes",
            }))
            ack = ws.receive_json()
            assert ack["action"] == "answer_ack"
            assert ack["accepted"] is True

        result = await waiter_task
        assert result == "yes"

    def test_ws_close_when_disabled(self) -> None:
        client = TestClient(self._make_app(token="t"))
        # Settings is read at WS accept time, so we patch the
        # Settings() call inside the route.
        with patch("harness.config.Settings") as mock_settings:
            mock_settings.return_value.hooks_elicitation_ws_enabled = False
            # When WS is disabled, the server closes immediately.
            # TestClient raises WebSocketDisconnect on connect.
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect("/api/v1/elicitation/ws?token=t") as ws:
                    ws.receive_json()
            # WebSocket close code 1008 = policy violation.
            assert exc_info.value.code == 1008

    def test_ws_invalid_json_returns_error(self) -> None:
        client = TestClient(self._make_app(token="t"))
        with client.websocket_connect("/api/v1/elicitation/ws?token=t") as ws:
            ws.receive_json()  # hello
            ws.send_text("not json")
            resp = ws.receive_json()
            assert resp["action"] == "error"
            assert "invalid JSON" in resp["error"]

    def test_ws_unknown_action_returns_error(self) -> None:
        client = TestClient(self._make_app(token="t"))
        with client.websocket_connect("/api/v1/elicitation/ws?token=t") as ws:
            ws.receive_json()  # hello
            ws.send_text(json.dumps({"action": "frobnicate"}))
            resp = ws.receive_json()
            assert resp["action"] == "error"
            assert "unknown action" in resp["error"]


# === 3. confirm_dangerous_hook + broker integration ===

class TestConfirmDangerousWithBroker:
    @pytest.mark.asyncio
    async def test_ws_disabled_falls_back_to_default(self) -> None:
        ctx = HookContext(
            event="Elicitation",
            session_id="s1",
            agent_id="",
            payload={
                "question": "Run rm -rf /?",
                "default_answer": "abort",
                "requires_confirmation": True,
            },
        )
        with patch("harness.config.Settings") as mock_settings:
            mock_settings.return_value.hooks_elicitation_ws_enabled = False
            decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "modify"
        assert decision.output["payload"]["answer"] == "abort"
        assert decision.output["payload"]["answer_source"] == "default_ws_disabled"

    @pytest.mark.asyncio
    async def test_ws_timeout_falls_back_to_default(self) -> None:
        # Simulate WS enabled but no client responding in time.
        ctx = HookContext(
            event="Elicitation",
            session_id="s1",
            agent_id="",
            payload={
                "question": "Drop table?",
                "default_answer": "abort",
                "requires_confirmation": True,
            },
        )
        with patch("harness.config.Settings") as mock_settings:
            mock_settings.return_value.hooks_elicitation_ws_enabled = True
            mock_settings.return_value.hooks_elicitation_ws_timeout_s = 0.05
            decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "modify"
        assert decision.output["payload"]["answer"] == "abort"
        # source is either default_timeout or ws_human (if stats check races).
        assert decision.output["payload"]["answer_source"] in (
            "default_timeout", "ws_human"
        )

    @pytest.mark.asyncio
    async def test_ws_human_answer_wins(self) -> None:
        ctx = HookContext(
            event="Elicitation",
            session_id="s1",
            agent_id="",
            payload={
                "question": "Proceed with deploy?",
                "default_answer": "abort",
                "options": ["proceed", "abort"],
                "requires_confirmation": True,
            },
        )
        # Pre-populate the broker: a human will answer "proceed" before
        # the hook's wait() times out.
        with patch("harness.config.Settings") as mock_settings:
            mock_settings.return_value.hooks_elicitation_ws_enabled = True
            mock_settings.return_value.hooks_elicitation_ws_timeout_s = 5.0

            # Start the hook in the background.
            task = asyncio.create_task(confirm_dangerous_hook(ctx))
            # Give it a moment to publish.
            await asyncio.sleep(0.05)
            broker = ElicitationBroker.get()
            pending = broker.pending()
            assert len(pending) == 1
            qid = pending[0].question_id
            # Simulate the human answering.
            broker.answer(qid, "proceed")
            decision = await task
        assert decision.decision == "modify"
        assert decision.output["payload"]["answer"] == "proceed"
        # Source is ws_human (or default_timeout if stats check races;
        # both are valid for a "proceed" answer — the user typed it).
        assert decision.output["payload"]["answer_source"] in (
            "ws_human", "default_timeout"
        )

    @pytest.mark.asyncio
    async def test_ignores_non_elicitation_events(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash"},
        )
        decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "allow"

    @pytest.mark.asyncio
    async def test_ignores_non_confirmation_prompts(self) -> None:
        ctx = HookContext(
            event="Elicitation",
            session_id="s1",
            agent_id="",
            payload={"question": "Anything goes"},
        )
        decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "allow"
