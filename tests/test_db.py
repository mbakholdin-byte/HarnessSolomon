"""Tests for SQLite store + JSONL mirror + rebuild.

Run: pytest tests/test_db.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import settings
from harness.server.db import sqlite as db_sqlite
from harness.server.db.models import Message, MessageUsage, ToolCall, ToolResult


@pytest.fixture
def tmp_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Override settings paths to a temp dir, ensure clean state."""
    data_dir = tmp_path / "harness-data"
    session_dir = data_dir / "sessions"
    db_path = data_dir / "harness.db"
    monkeypatch.setattr(settings, "session_dir", session_dir)
    monkeypatch.setattr(settings, "db_path", db_path)
    # Reset module-level init flag so init_db uses new path
    db_sqlite._db_initialized = False
    yield data_dir
    # Cleanup is automatic via tmp_path


async def test_session_roundtrip(tmp_data: Path) -> None:
    """Create, get, list, delete session."""
    s = await db_sqlite.create_session(title="hello", model="MiniMax-M2.7")
    assert s.id
    assert s.title == "hello"
    assert s.model == "MiniMax-M2.7"

    got = await db_sqlite.get_session(s.id)
    assert got is not None
    assert got.id == s.id
    assert got.title == "hello"

    sessions = await db_sqlite.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == s.id

    deleted = await db_sqlite.delete_session(s.id)
    assert deleted is True
    assert await db_sqlite.get_session(s.id) is None


async def test_message_roundtrip(tmp_data: Path) -> None:
    """Add message, list messages, JSONL mirror present."""
    s = await db_sqlite.create_session(title="m", model="MiniMax-M2.7")
    msg = Message(
        session_id=s.id,
        role="user",
        content="Привет",
    )
    await db_sqlite.add_message(msg)
    await db_sqlite.touch_session(s.id, message_count_delta=1)

    msgs = await db_sqlite.list_messages(s.id)
    assert len(msgs) == 1
    assert msgs[0].content == "Привет"
    assert msgs[0].role == "user"

    # JSONL mirror
    jsonl_path = db_sqlite.append_jsonl(msg)
    assert jsonl_path.exists()
    with jsonl_path.open("r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    assert len(lines) == 1
    parsed = Message.from_jsonl(lines[0])
    assert parsed.content == "Привет"


async def test_tool_call_serialization(tmp_data: Path) -> None:
    """ToolCall/ToolResult roundtrip via Pydantic + JSONL."""
    tc = ToolCall(id="tc1", name="read_file", arguments={"path": "/tmp/x"})
    tr = ToolResult(id="tc1", output="hello", ok=True)
    usage = MessageUsage(input_tokens=10, output_tokens=20, cost=0.001)

    msg = Message(
        session_id="s1",
        role="assistant",
        content="",
        tool_calls=[tc],
        tool_results=[tr],
        model="MiniMax-M2.7",
        usage=usage,
    )
    line = msg.to_jsonl()
    parsed = Message.from_jsonl(line)
    assert parsed.tool_calls is not None
    assert parsed.tool_calls[0].name == "read_file"
    assert parsed.tool_calls[0].arguments == {"path": "/tmp/x"}
    assert parsed.tool_results is not None
    assert parsed.tool_results[0].output == "hello"
    assert parsed.usage is not None
    assert parsed.usage.input_tokens == 10


async def test_rebuild_from_jsonl(tmp_data: Path) -> None:
    """Rebuild SQLite index from JSONL files."""
    # 1. Create JSONL manually
    session_id = "sess-rebuild-1"
    msgs = [
        Message(session_id=session_id, role="user", content="первое сообщение"),
        Message(
            session_id=session_id,
            role="assistant",
            content="ответ",
            model="MiniMax-M2.7",
            usage=MessageUsage(input_tokens=5, output_tokens=10, cost=0.0001),
        ),
        Message(session_id=session_id, role="user", content="второе"),
    ]
    jsonl_path = settings.session_dir / f"{session_id}.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(m.to_jsonl() + "\n")

    # 2. Rebuild
    count = await db_sqlite.rebuild_from_jsonl()
    assert count == 1

    # 3. Verify
    s = await db_sqlite.get_session(session_id)
    assert s is not None
    assert s.title == "первое сообщение"  # First user msg, truncated to 80 chars
    assert s.model == "MiniMax-M2.7"
    assert s.message_count == 3
    assert s.total_tokens == 15
    assert abs(s.total_cost - 0.0001) < 1e-9

    listed_msgs = await db_sqlite.list_messages(session_id)
    assert len(listed_msgs) == 3
    assert [m.role for m in listed_msgs] == ["user", "assistant", "user"]


async def test_delete_session_cascades_messages(tmp_data: Path) -> None:
    """Deleting session removes its messages (FK cascade)."""
    s = await db_sqlite.create_session(title="del", model="MiniMax-M2.7")
    for i in range(3):
        await db_sqlite.add_message(
            Message(session_id=s.id, role="user", content=f"msg {i}")
        )

    assert len(await db_sqlite.list_messages(s.id)) == 3
    await db_sqlite.delete_session(s.id)
    assert len(await db_sqlite.list_messages(s.id)) == 0
