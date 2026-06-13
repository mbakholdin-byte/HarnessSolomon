"""Solomon Harness — async SQLite store for session metadata.

JSONL files in session_dir are the source of truth.
SQLite is the index for fast listing/lookup; rebuildable from JSONL.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from harness.config import settings
from harness.server.db.models import Message, Session


def _utcnow() -> datetime:
    """UTC now without timezone (SQLite stores naive ISO)."""
    return datetime.now(UTC).replace(tzinfo=None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls TEXT,
    tool_results TEXT,
    model TEXT,
    usage TEXT,
    ts TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def _row_to_session(row: tuple[Any, ...]) -> Session:
    return Session(
        id=row[0],
        title=row[1],
        model=row[2],
        created_at=_parse(row[3]),
        updated_at=_parse(row[4]),
        message_count=row[5],
        total_tokens=row[6],
        total_cost=row[7],
    )


def _row_to_message(row: tuple[Any, ...]) -> Message:
    return Message(
        id=row[0],
        session_id=row[1],
        role=row[2],  # type: ignore[arg-type]
        content=row[3],
        tool_calls=json.loads(row[4]) if row[4] else None,
        tool_results=json.loads(row[5]) if row[5] else None,
        model=row[6],
        usage=json.loads(row[7]) if row[7] else None,
        ts=_parse(row[8]),
    )


# === Init ===

_db_initialized = False


async def init_db() -> None:
    """Create tables if not exist. Idempotent."""
    global _db_initialized
    if _db_initialized:
        return
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(SCHEMA)
        await db.commit()
    _db_initialized = True


async def rebuild_from_jsonl() -> int:
    """Rebuild SQLite from JSONL files. Returns count of sessions rebuilt.

    Use this on startup if SQLite is missing or corrupt. JSONL is the truth.
    """
    await init_db()
    settings.session_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        # Wipe existing data
        await db.execute("DELETE FROM messages")
        await db.execute("DELETE FROM sessions")

        for jsonl_path in sorted(settings.session_dir.glob("*.jsonl")):
            session_id = jsonl_path.stem
            messages: list[Message] = []
            with jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = Message.from_jsonl(line)
                        messages.append(msg)
                    except Exception:
                        # Skip malformed lines, do not fail the whole rebuild
                        continue

            if not messages:
                continue

            # First user message is the session title seed
            first_user = next((m for m in messages if m.role == "user"), messages[0])
            title = first_user.content[:80] if first_user.content else "Untitled"
            # Find the assistant model
            model = next(
                (m.model for m in messages if m.role == "assistant" and m.model),
                "unknown",
            )
            created = messages[0].ts
            updated = messages[-1].ts
            total_tokens = sum(
                (m.usage.input_tokens + m.usage.output_tokens)
                for m in messages
                if m.usage
            )
            total_cost = sum(
                m.usage.cost for m in messages if m.usage
            )

            await db.execute(
                """INSERT INTO sessions
                   (id, title, model, created_at, updated_at, message_count, total_tokens, total_cost)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    title,
                    model,
                    _iso(created),
                    _iso(updated),
                    len(messages),
                    total_tokens,
                    total_cost,
                ),
            )
            for m in messages:
                await db.execute(
                    """INSERT INTO messages
                       (id, session_id, role, content, tool_calls, tool_results, model, usage, ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        m.id,
                        m.session_id,
                        m.role,
                        m.content,
                        json.dumps([tc.model_dump() for tc in m.tool_calls])
                        if m.tool_calls
                        else None,
                        json.dumps([tr.model_dump() for tr in m.tool_results])
                        if m.tool_results
                        else None,
                        m.model,
                        json.dumps(m.usage.model_dump()) if m.usage else None,
                        _iso(m.ts),
                    ),
                )
            count += 1

        await db.commit()
    return count


# === Sessions CRUD ===

async def list_sessions(limit: int = 50) -> list[Session]:
    """List most recently updated sessions."""
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [_row_to_session(tuple(row)) for row in await cur.fetchall()]


async def get_session(session_id: str) -> Session | None:
    """Get a session by id."""
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
            return _row_to_session(row) if row else None


async def create_session(title: str, model: str) -> Session:
    """Create new session, return it."""
    await init_db()
    session = Session(title=title, model=model)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """INSERT INTO sessions
               (id, title, model, created_at, updated_at, message_count, total_tokens, total_cost)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                session.title,
                session.model,
                _iso(session.created_at),
                _iso(session.updated_at),
                0,
                0,
                0.0,
            ),
        )
        await db.commit()
    return session


async def delete_session(session_id: str) -> bool:
    """Delete session + cascade messages. Returns True if existed."""
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()
        return cur.rowcount > 0


async def touch_session(session_id: str, message_count_delta: int = 1) -> None:
    """Update updated_at and increment message_count + totals."""
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """UPDATE sessions
               SET updated_at = ?, message_count = message_count + ?
               WHERE id = ?""",
            (_iso(_utcnow()), message_count_delta, session_id),
        )
        await db.commit()


# === Messages ===

async def add_message(msg: Message) -> None:
    """Insert a message."""
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            """INSERT INTO messages
               (id, session_id, role, content, tool_calls, tool_results, model, usage, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.id,
                msg.session_id,
                msg.role,
                msg.content,
                json.dumps([tc.model_dump() for tc in msg.tool_calls])
                if msg.tool_calls
                else None,
                json.dumps([tr.model_dump() for tr in msg.tool_results])
                if msg.tool_results
                else None,
                msg.model,
                json.dumps(msg.usage.model_dump()) if msg.usage else None,
                _iso(msg.ts),
            ),
        )
        await db.commit()


async def list_messages(session_id: str) -> list[Message]:
    """All messages in a session, in order."""
    await init_db()
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY ts ASC",
            (session_id,),
        ) as cur:
            return [_row_to_message(tuple(row)) for row in await cur.fetchall()]


# === JSONL mirror ===

def append_jsonl(msg: Message) -> Path:
    """Append message to JSONL file for the session. Returns the path."""
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = settings.session_dir / f"{msg.session_id}.jsonl"
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(msg.to_jsonl() + "\n")
    return jsonl_path
