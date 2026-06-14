"""Tests for the WebSocket chat endpoint (Шаг 7, Phase 0).

Per TDD (RED → GREEN → REFACTOR): tests first, then minimal implementation.

Endpoint under test:
  WS /api/chat/ws?session_id=...&model=...

Protocol (server → client JSON events):
  - {"type": "assistant_message", "content": ..., "usage": ..., "cost": ...}
  - {"type": "tool_result", "content": ..., "tool_call": {...}}
  - {"type": "error", "content": "..."}
  - {"type": "done"}
  - {"type": "session_done"}

Protocol (client → server JSON):
  - {"type": "user_message", "content": "..."}

The tests use ``TestClient`` (which supports WebSocket via
``with client.websocket_connect(...) as ws``) and a patched
``LLMRouter`` so we don't hit any real LLM API.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from harness.config import settings
from harness.server.agent.session import ChatSession
from harness.server.app import create_app
from harness.server.db import sqlite as db_sqlite
from harness.server.llm.router import CompletionResult


# === Fakes ===

class FakeRouter:
    """Fake ``LLMRouter`` for WebSocket tests.

    Mirrors the FakeRouter used in ``test_agent_loop.py`` but exposes the
    same public surface as ``LLMRouter`` (an async ``completion`` method
    that returns a ``CompletionResult``). We don't subclass or mock the
    real router — the agent loop only calls ``router.completion(...)``
    so any object with that method works.
    """

    def __init__(self, scripted_responses: list[CompletionResult]) -> None:
        self.scripted_responses = scripted_responses
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    async def completion(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        self.calls.append(
            {"messages": list(messages), "model": model, "tools": tools}
        )
        if self.call_count >= len(self.scripted_responses):
            raise RuntimeError("FakeRouter: out of scripted responses")
        resp = self.scripted_responses[self.call_count]
        self.call_count += 1
        return resp


# === Fixtures ===

@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """Test client with isolated data dir + isolated project_root."""
    data_dir = tmp_path / "harness-data"
    project_root = tmp_path / "ws-project-root"
    project_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "session_dir", data_dir / "sessions")
    monkeypatch.setattr(settings, "db_path", data_dir / "harness.db")
    monkeypatch.setattr(settings, "project_root", project_root)
    db_sqlite._db_initialized = False

    app = create_app()
    return TestClient(app)


def _create_session(client: TestClient, title: str = "ws-test", model: str = "MiniMax-M2.7") -> str:
    """Helper: create a session via REST and return its id."""
    r = client.post("/api/sessions", json={"title": title, "model": model})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# === Test 1: connect, send user_message, get session_done ===

def test_ws_connect_and_get_session_done(client: TestClient) -> None:
    """Happy path: send a user_message, get assistant_message + session_done."""
    sid = _create_session(client)

    fake = FakeRouter(
        scripted_responses=[
            CompletionResult(
                content="Hello back!",
                tool_calls=None,
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                cost=0.0,
            )
        ]
    )
    with patch("harness.server.routes.chat.LLMRouter", return_value=fake):
        with client.websocket_connect(
            f"/api/chat/ws?session_id={sid}&model=MiniMax-M2.7"
        ) as ws:
            ws.send_json({"type": "user_message", "content": "Привет"})
            events: list[dict] = []
            for msg in ws.iter_text():
                payload = json.loads(msg)
                events.append(payload)
                if payload.get("type") == "session_done":
                    break

    types = [e.get("type") for e in events]
    assert "assistant_message" in types
    assert "session_done" in types
    # session_done is the LAST event
    assert types[-1] == "session_done"


# === Test 2: stream contains assistant_message ===

def test_ws_streams_assistant_message(client: TestClient) -> None:
    """At least one event with type='assistant_message' carrying content."""
    sid = _create_session(client)

    fake = FakeRouter(
        scripted_responses=[
            CompletionResult(
                content="Streaming!",
                tool_calls=None,
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                cost=0.0,
            )
        ]
    )
    with patch("harness.server.routes.chat.LLMRouter", return_value=fake):
        with client.websocket_connect(
            f"/api/chat/ws?session_id={sid}&model=MiniMax-M2.7"
        ) as ws:
            ws.send_json({"type": "user_message", "content": "hi"})
            events: list[dict] = []
            for msg in ws.iter_text():
                payload = json.loads(msg)
                events.append(payload)
                if payload.get("type") == "session_done":
                    break

    assistant_events = [e for e in events if e.get("type") == "assistant_message"]
    assert len(assistant_events) >= 1
    assert any(e.get("content") == "Streaming!" for e in assistant_events)


# === Test 3: user_message is persisted to DB ===

def test_ws_persists_user_message_to_db(client: TestClient) -> None:
    """After sending a user_message, it must be retrievable via the REST API."""
    sid = _create_session(client)

    fake = FakeRouter(
        scripted_responses=[
            CompletionResult(
                content="ok",
                tool_calls=None,
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                cost=0.0,
            )
        ]
    )
    with patch("harness.server.routes.chat.LLMRouter", return_value=fake):
        with client.websocket_connect(
            f"/api/chat/ws?session_id={sid}&model=MiniMax-M2.7"
        ) as ws:
            ws.send_json({"type": "user_message", "content": "persisted!"})
            for msg in ws.iter_text():
                if json.loads(msg).get("type") == "session_done":
                    break

    # Verify persistence via the REST endpoint
    r = client.get(f"/api/sessions/{sid}/messages")
    assert r.status_code == 200
    msgs = r.json()
    user_msgs = [m for m in msgs if m["role"] == "user" and m["content"] == "persisted!"]
    assert len(user_msgs) == 1

    # Assistant message should also be persisted
    assistant_msgs = [m for m in msgs if m["role"] == "assistant" and m["content"] == "ok"]
    assert len(assistant_msgs) == 1


# === Test 4: unknown session → error event ===

def test_ws_handles_unknown_session(client: TestClient) -> None:
    """Connecting with a bogus session_id should yield an error event and close."""
    fake = FakeRouter(scripted_responses=[])
    with patch("harness.server.routes.chat.LLMRouter", return_value=fake):
        # The server sends error then closes. We use a one-shot read.
        with client.websocket_connect(
            "/api/chat/ws?session_id=nonexistent-session&model=MiniMax-M2.7"
        ) as ws:
            events: list[dict] = []
            for msg in ws.iter_text():
                payload = json.loads(msg)
                events.append(payload)
                if payload.get("type") == "error":
                    break

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "session" in events[0].get("content", "").lower()


# === Test 5: unknown model → error event ===

def test_ws_unknown_model(client: TestClient) -> None:
    """Connecting with a bogus model id should yield an error event and close."""
    sid = _create_session(client)
    fake = FakeRouter(scripted_responses=[])
    with patch("harness.server.routes.chat.LLMRouter", return_value=fake):
        with client.websocket_connect(
            f"/api/chat/ws?session_id={sid}&model=does-not-exist"
        ) as ws:
            events: list[dict] = []
            for msg in ws.iter_text():
                payload = json.loads(msg)
                events.append(payload)
                if payload.get("type") == "error":
                    break

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "model" in events[0].get("content", "").lower()


# === Test 6 (optional): tool_call visibility ===

def test_ws_tool_call_visible(client: TestClient) -> None:
    """When the LLM calls a tool, the tool_result event is forwarded and persisted."""
    sid = _create_session(client)

    # Pre-create a file the tool will read
    (settings.project_root / "ws-tool-target.txt").write_text(
        "tool-output-ok", encoding="utf-8"
    )

    fake = FakeRouter(
        scripted_responses=[
            # First call: LLM asks to read the file
            CompletionResult(
                content="Reading...",
                tool_calls=[
                    {
                        "id": "call_ws_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "ws-tool-target.txt"}),
                        },
                    }
                ],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            ),
            # Second call: final answer after seeing the tool result
            CompletionResult(
                content="Done",
                tool_calls=None,
                usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            ),
        ]
    )
    with patch("harness.server.routes.chat.LLMRouter", return_value=fake):
        with client.websocket_connect(
            f"/api/chat/ws?session_id={sid}&model=MiniMax-M2.7"
        ) as ws:
            ws.send_json({"type": "user_message", "content": "read it"})
            events: list[dict] = []
            for msg in ws.iter_text():
                payload = json.loads(msg)
                events.append(payload)
                if payload.get("type") == "session_done":
                    break

    types = [e.get("type") for e in events]
    assert "tool_result" in types
    tool_results = [e for e in events if e.get("type") == "tool_result"]
    assert any("tool-output-ok" in e.get("content", "") for e in tool_results)
    # Two assistant_message events (one per LLM turn)
    assert types.count("assistant_message") >= 1
    # The tool_result must carry a tool_call envelope
    tr = tool_results[0]
    assert tr["tool_call"]["name"] == "read_file"
    assert tr["tool_call"]["ok"] is True


# === Unit tests for ChatSession (the wrapper itself) ===

async def test_chat_session_loads_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ChatSession.load_history returns messages in the right shape."""
    data_dir = tmp_path / "harness-data"
    monkeypatch.setattr(settings, "session_dir", data_dir / "sessions")
    monkeypatch.setattr(settings, "db_path", data_dir / "harness.db")
    db_sqlite._db_initialized = False

    # Create session + add a message via the lower-level API
    session = await db_sqlite.create_session(title="x", model="MiniMax-M2.7")
    from harness.server.db.models import Message
    m1 = Message(session_id=session.id, role="user", content="hi there")
    await db_sqlite.add_message(m1)

    chat = ChatSession(
        session_id=session.id,
        model="MiniMax-M2.7",
        db=db_sqlite,
        project_root=tmp_path,
    )
    history = await chat.load_history()
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hi there"


async def test_chat_session_add_message_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ChatSession.add_message writes to SQLite + JSONL and touches the session."""
    data_dir = tmp_path / "harness-data"
    monkeypatch.setattr(settings, "session_dir", data_dir / "sessions")
    monkeypatch.setattr(settings, "db_path", data_dir / "harness.db")
    db_sqlite._db_initialized = False

    session = await db_sqlite.create_session(title="x", model="MiniMax-M2.7")
    chat = ChatSession(
        session_id=session.id,
        model="MiniMax-M2.7",
        db=db_sqlite,
        project_root=tmp_path,
    )
    msg = await chat.add_message("user", "hello world")
    assert msg.role == "user"
    assert msg.content == "hello world"
    assert msg.session_id == session.id

    # Verify in DB
    msgs = await db_sqlite.list_messages(session.id)
    assert len(msgs) == 1
    assert msgs[0].content == "hello world"

    # Verify JSONL mirror exists
    jsonl = settings.session_dir / f"{session.id}.jsonl"
    assert jsonl.exists()
    content = jsonl.read_text(encoding="utf-8").strip()
    assert "hello world" in content

    # Verify session was touched
    touched = await db_sqlite.get_session(session.id)
    assert touched is not None
    assert touched.message_count == 1
