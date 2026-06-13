"""Sessions REST API.

Endpoints (under /api):
  GET    /sessions                 — list sessions (most recent first)
  POST   /sessions                 — create session
  GET    /sessions/{id}            — get session by id
  DELETE /sessions/{id}            — delete session + cascade messages
  GET    /sessions/{id}/messages   — list messages for session
  POST   /sessions/{id}/messages   — add message to session
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from harness.server.db import sqlite as db_sqlite
from harness.server.db.models import Message

router = APIRouter()


# === Request/Response schemas ===

class CreateSessionRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    model: str = Field(..., min_length=1, max_length=100)


class AddMessageRequest(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str
    model: str | None = None  # for assistant
    tool_calls: list[dict] | None = None
    tool_results: list[dict] | None = None


# === Endpoints ===

@router.get("/sessions")
async def list_sessions() -> list[dict]:
    """List most recent sessions."""
    sessions = await db_sqlite.list_sessions(limit=50)
    return [_session_to_dict(s) for s in sessions]


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(req: CreateSessionRequest) -> dict:
    """Create new session."""
    session = await db_sqlite.create_session(title=req.title, model=req.model)
    return _session_to_dict(session)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    """Get session by id."""
    session = await db_sqlite.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_to_dict(session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str) -> None:
    """Delete session + cascade messages."""
    deleted = await db_sqlite.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str) -> list[dict]:
    """List messages for a session, in order."""
    # Verify session exists (otherwise return empty)
    session = await db_sqlite.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = await db_sqlite.list_messages(session_id)
    return [_message_to_dict(m) for m in messages]


@router.post(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_201_CREATED,
)
async def add_message(session_id: str, req: AddMessageRequest) -> dict:
    """Add a message to a session.

    Also touches the session (updated_at + message_count++) and mirrors to JSONL.
    """
    session = await db_sqlite.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    msg = Message(
        session_id=session_id,
        role=req.role,
        content=req.content,
        model=req.model,
        tool_calls=req.tool_calls,  # type: ignore[arg-type]
        tool_results=req.tool_results,  # type: ignore[arg-type]
    )
    await db_sqlite.add_message(msg)
    db_sqlite.append_jsonl(msg)
    await db_sqlite.touch_session(session_id, message_count_delta=1)

    return _message_to_dict(msg)


# === Serialization helpers ===

def _session_to_dict(s) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "model": s.model,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
        "message_count": s.message_count,
        "total_tokens": s.total_tokens,
        "total_cost": s.total_cost,
    }


def _message_to_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "session_id": m.session_id,
        "role": m.role,
        "content": m.content,
        "tool_calls": [tc.model_dump() for tc in m.tool_calls] if m.tool_calls else None,
        "tool_results": [tr.model_dump() for tr in m.tool_results] if m.tool_results else None,
        "model": m.model,
        "ts": m.ts.isoformat(),
    }
