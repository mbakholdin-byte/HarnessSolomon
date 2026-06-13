"""Solomon Harness — domain models (Pydantic v2).

Session and Message are the two entities. JSONL mirror is source of truth;
SQLite is the index for fast queries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    """UTC now (no timezone for SQLite compat)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _uuid() -> str:
    """UUID4 string."""
    return str(uuid4())


class Session(BaseModel):
    """A chat session — one conversation thread with one model."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_uuid)
    title: str
    model: str
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    message_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0


class ToolCall(BaseModel):
    """A single tool invocation requested by the LLM."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    """Result of a tool execution."""

    model_config = ConfigDict(extra="ignore")

    id: str  # matches ToolCall.id
    output: str
    ok: bool = True
    error: str | None = None


class MessageUsage(BaseModel):
    """Token usage + cost for an assistant message."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


class Message(BaseModel):
    """A single message in a session.

    Role semantics:
      - 'user':      human input
      - 'assistant': LLM response (may include tool_calls)
      - 'tool':      tool execution result (paired with assistant tool_calls by id)
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_uuid)
    session_id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_results: list[ToolResult] | None = None
    model: str | None = None  # for assistant
    usage: MessageUsage | None = None
    ts: datetime = Field(default_factory=_now)

    def to_jsonl(self) -> str:
        """Serialize to single-line JSON for JSONL mirror."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> "Message":
        """Deserialize from single-line JSON."""
        return cls.model_validate_json(line)
