"""Chat session wrapper (Шаг 7).

A thin convenience layer over ``harness.server.db.sqlite`` that:

  * Loads the full message history for a session as an OpenAI-style
    list-of-dicts (the format the agent loop expects).
  * Persists new messages (DB + JSONL) and updates session totals in
    one call.
  * Lists sessions (static method, sugar for ``db_sqlite.list_sessions``).

The wrapper does NOT own the LLM call, the tool execution, or the
agent loop. It is intentionally dumb: just persistence + history
shaping. The WebSocket route glues this together with ``AgentLoop``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.server.db import sqlite as db_sqlite
from harness.server.db.models import Message, MessageUsage, Session, ToolCall, ToolResult

if TYPE_CHECKING:
    from harness.context.compaction import ContextCompactor

logger = logging.getLogger(__name__)


class ChatSession:
    """Per-WebSocket session handle.

    Attributes:
        session_id:  UUID of the session row in SQLite.
        model:       Model id from the catalog (e.g. "MiniMax-M2.7").
        db:          Reference to the ``db_sqlite`` module (kept as a
                     module-level singleton — every ChatSession writes
                     to the same database file).
        project_root: Path used by tool calls. Held here for symmetry
                     with ``AgentLoop`` + ``ToolRuntime`` even though
                     the wrapper itself does not touch the filesystem.
    """

    def __init__(
        self,
        session_id: str,
        model: str,
        db: Any,
        project_root: Path,
        compactor: "ContextCompactor | None" = None,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.db = db
        self.project_root = project_root
        # Phase 3: optional compactor applied on history load. Default
        # None → no-op (preserves pre-Phase-3 behaviour: load full
        # history). Injected by ``server.app.lifespan`` when
        # ``settings.compaction_enabled`` is True.
        self.compactor = compactor

    # --- history ---

    async def load_history(self) -> list[dict[str, Any]]:
        """Load all messages for this session as OpenAI-style dicts.

        Shape per message:
          - ``role`` is one of "user" / "assistant" / "tool"
          - ``content`` is the text content
          - For assistant turns with tool calls: includes ``tool_calls``
            in the router's normalised shape (id, type, function)
          - For tool turns: includes ``tool_call_id`` and (when
            recoverable) ``name`` alongside ``content`` so the LLM
            can correlate the result with the originating call.
        """
        messages = await self.db.list_messages(self.session_id)

        # Build a lookup: tool_call_id -> name from the nearest
        # preceding assistant message. Used to enrich tool messages.
        id_to_name: dict[str, str] = {}
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    if tc.id:
                        id_to_name[tc.id] = tc.name

        history: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {
                "role": m.role,
                "content": m.content,
            }
            if m.role == "assistant" and m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": (
                                json_dumps(tc.arguments)
                                if not isinstance(tc.arguments, str)
                                else tc.arguments
                            ),
                        },
                    }
                    for tc in m.tool_calls
                ]
            if m.role == "tool" and m.tool_results and m.tool_results[0].id:
                entry["tool_call_id"] = m.tool_results[0].id
                name = id_to_name.get(m.tool_results[0].id)
                if name:
                    entry["name"] = name
            history.append(entry)
        # Phase 3: compact the loaded history before returning if a
        # compactor was injected. The compactor returns a NEW list;
        # the persisted DB rows are unchanged (compaction is in-memory
        # only — JSONL mirror and SQLite retain the full history, see
        # Phase 3 plan §10 "JSONL session mirror rewrites").
        if self.compactor is not None:
            history = await self.compactor.maybe_compact(history, self.model)
        return history

    # --- persistence ---

    async def add_message(
        self,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_results: list[ToolResult] | None = None,
        usage: MessageUsage | None = None,
    ) -> Message:
        """Persist a message to SQLite + JSONL mirror, then touch the session.

        Args:
            role:          "user" | "assistant" | "tool"
            content:       The message text.
            tool_call_id:  For "tool" messages, the originating call id.
            tool_name:     For "tool" messages, the originating tool name.
            tool_calls:    For "assistant" messages, list of dict-shaped
                           tool calls (router normalised format).
            tool_results:  For "tool" messages, the structured result(s).
            usage:         For "assistant" messages, the token usage block.

        Returns:
            The persisted ``Message`` model (with id + ts).
        """
        # Build the structured tool_calls / tool_results so the DB layer
        # can store them as JSON.
        structured_tool_calls: list[ToolCall] | None = None
        if tool_calls:
            structured_tool_calls = []
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or ""
                raw_args = fn.get("arguments") or tc.get("arguments") or "{}"
                if isinstance(raw_args, str):
                    import json
                    try:
                        args_obj = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args_obj = {"_raw": raw_args}
                else:
                    args_obj = raw_args
                structured_tool_calls.append(
                    ToolCall(
                        id=str(tc.get("id") or ""),
                        name=str(name),
                        arguments=args_obj if isinstance(args_obj, dict) else {},
                    )
                )

        structured_tool_results: list[ToolResult] | None = tool_results
        if structured_tool_results is None and role == "tool" and tool_call_id:
            # Default-encode a single tool result.
            structured_tool_results = [
                ToolResult(id=tool_call_id, output=content, ok=True, error=None)
            ]

        msg = Message(
            session_id=self.session_id,
            role=role,  # type: ignore[arg-type]
            content=content,
            tool_calls=structured_tool_calls,
            tool_results=structured_tool_results,
            model=self.model if role == "assistant" else None,
            usage=usage,
        )
        await self.db.add_message(msg)
        self.db.append_jsonl(msg)
        await self.db.touch_session(self.session_id, message_count_delta=1)
        logger.debug(
            "ChatSession.add_message: session=%s role=%s id=%s",
            self.session_id,
            role,
            msg.id,
        )
        return msg

    # --- list ---

    @staticmethod
    async def list_sessions(limit: int = 50) -> list[Session]:
        """Wrapper around ``db_sqlite.list_sessions``."""
        return await db_sqlite.list_sessions(limit=limit)


# === helpers ===

def json_dumps(obj: Any) -> str:
    """Cheap JSON helper that avoids a top-level json import for the
    common path (load_history). Imported lazily inside hot paths when
    needed.
    """
    import json

    return json.dumps(obj, ensure_ascii=False)


__all__ = ["ChatSession"]
