"""CompactStore — persistent storage for compaction summaries (Phase 3.5, Step 0).

Phase 3 (v1.0.0) introduced :class:`~harness.context.compaction.ContextCompactor`
which performs an **in-memory** sliding-window + LLM summary on every
``load_history`` call. That works for short sessions but is wasteful for
long ones: the same history is re-summarised on every WebSocket reconnect,
and the only persistent record is a single L2-mem0 row tagged ``#compact``
with no version history.

Phase 3.5 (v1.1.0) introduces this ``CompactStore`` — a small SQLite table
in the existing ``agent-jobs.db`` (sibling of ``merge_jobs`` /
``merge_events`` / ``webhook_events``). The store holds one row per
``(session_id, version)`` pair and lets the compactor:

  1. **Cache hit** — look up a compact by ``source_hash`` and return it
     without calling the LLM summariser (zero cost on reconnect).
  2. **Audit trail** — every compact has metrics (``original_tokens``,
     ``compacted_tokens``, ``model``, ``duration_ms``, ``outcome``)
     that an operator can query for observability.
  3. **Reconstruction** — ``kept_message_ids`` (JSON list) lets the
     compactor rebuild the kept window from the underlying session
     DB without storing the full message payload (200KB+ per row).

The store follows the same pattern as
:class:`~harness.agents.jobs.JobStore` (idempotent ``CREATE TABLE IF NOT
EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` migrations, ``aiosqlite``
async access, fail-open semantics in the caller).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# === Schema ===

#: Outcome values stored in ``compact_store.outcome``. Phase 3.5 only
#: produces ``"ok"`` (summariser succeeded) — ``"fallback"`` and
#: ``"fail"`` are reserved for future enhancements (T1+T2 both down).
OUTCOMES: tuple[str, ...] = ("ok", "fallback", "fail")

#: Trigger kinds stored in ``compact_store.trigger_kind``. Phase 3.5
#: only triggers from ``"auto_load_history"`` (no API endpoint yet,
#: no background worker).
TRIGGER_KINDS: tuple[str, ...] = ("auto_load_history",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compact_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    original_tokens INTEGER NOT NULL,
    compacted_tokens INTEGER NOT NULL,
    original_message_count INTEGER NOT NULL,
    kept_message_ids TEXT NOT NULL,
    summary TEXT NOT NULL,
    model TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at REAL NOT NULL,
    duration_ms REAL NOT NULL,
    UNIQUE(session_id, version)
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_compact_store_session_recent
    ON compact_store(session_id, version DESC)
"""

_INDEX_HASH = """
CREATE INDEX IF NOT EXISTS idx_compact_store_session_hash
    ON compact_store(session_id, source_hash)
"""


# === Record dataclass ===

@dataclass(slots=True)
class CompactRecord:
    """One row of the ``compact_store`` table.

    Fields mirror the SQL columns 1:1. ``kept_message_ids`` is stored as
    a JSON string in SQLite and decoded to ``list[int]`` on read.
    """

    session_id: str
    version: int
    source_hash: str
    original_tokens: int
    compacted_tokens: int
    original_message_count: int
    kept_message_ids: list[int]
    summary: str
    model: str
    trigger_kind: str
    outcome: str
    created_at: float
    duration_ms: float

    def to_row(self) -> dict[str, Any]:
        """Return a dict ready to be passed to ``INSERT INTO``."""
        return {
            "session_id": self.session_id,
            "version": self.version,
            "source_hash": self.source_hash,
            "original_tokens": self.original_tokens,
            "compacted_tokens": self.compacted_tokens,
            "original_message_count": self.original_message_count,
            "kept_message_ids": json.dumps(self.kept_message_ids),
            "summary": self.summary,
            "model": self.model,
            "trigger_kind": self.trigger_kind,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> CompactRecord:
        """Build a ``CompactRecord`` from an ``aiosqlite`` row.

        ``aiosqlite.Row`` supports dict-style access, so both row
        types work with the same code path.
        """
        kept = row["kept_message_ids"]
        if isinstance(kept, str):
            kept_ids = json.loads(kept)
        else:
            kept_ids = list(kept)
        return cls(
            session_id=row["session_id"],
            version=row["version"],
            source_hash=row["source_hash"],
            original_tokens=row["original_tokens"],
            compacted_tokens=row["compacted_tokens"],
            original_message_count=row["original_message_count"],
            kept_message_ids=kept_ids,
            summary=row["summary"],
            model=row["model"],
            trigger_kind=row["trigger_kind"],
            outcome=row["outcome"],
            created_at=row["created_at"],
            duration_ms=row["duration_ms"],
        )


# === Store ===

class CompactStore:
    """Persistent cache for :class:`~harness.context.compaction.ContextCompactor`.

    All methods are async (the compactor is async and we want to avoid
    blocking the event loop on SQLite I/O). The store is **not**
    thread-safe by itself — aiosqlite serialises access per connection,
    and concurrent writers on the same file use WAL + ``busy_timeout``
    to coordinate (see :meth:`init`).

    Parameters
    ----------
    db_path:
        Path to the SQLite file. Phase 3.5 reuses the existing
        ``agent-jobs.db`` (sibling of ``harness.db``) so the compactor
        shares the same WAL/connection-pool lifecycle as
        :class:`~harness.agents.jobs.JobStore`.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._initialized = False

    async def init(self) -> None:
        """Create the ``compact_store`` table and indexes if missing.

        Idempotent — safe to call on every startup. The first call
        creates the schema; subsequent calls are no-ops.
        """
        if self._initialized:
            return
        # Ensure parent dir exists (sibling of harness.db, may not
        # exist on a fresh install if no job has ever run).
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            # WAL mode + busy_timeout match JobStore defaults (see
            # ``harness/agents/jobs.py``) so the two stores don't
            # lock-step on a busy server.
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(_SCHEMA)
            await db.execute(_INDEX)
            await db.execute(_INDEX_HASH)
            await db.commit()
        self._initialized = True
        logger.info("CompactStore ready at %s", self._db_path)

    async def lookup_cached(
        self,
        session_id: str,
        source_hash: str,
    ) -> CompactRecord | None:
        """Find the latest cached compact for a given session+source.

        Returns ``None`` on cache miss (no row, or all rows have a
        different ``source_hash``). The caller is expected to fall
        through to the slow path (re-summarise) on ``None``.
        """
        if not self._initialized:
            await self.init()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(
                "SELECT * FROM compact_store "
                "WHERE session_id = ? AND source_hash = ? "
                "ORDER BY version DESC LIMIT 1",
                (session_id, source_hash),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return CompactRecord.from_row(row)

    async def insert(self, record: CompactRecord) -> int:
        """Persist a new compact record. Returns the assigned version.

        ``version`` is auto-computed as ``MAX(version)+1`` for the
        session (or 1 for the first compact of a new session). The
        caller's ``record.version`` is overwritten with the assigned
        value to make round-tripping the record easy.

        Raises
        ------
        sqlite3.IntegrityError
            If a row with the same ``(session_id, version)`` already
            exists (concurrent insert race). Caller should retry with
            a fresh record.
        """
        if not self._initialized:
            await self.init()
        async with aiosqlite.connect(self._db_path) as db:
            # Compute next version under the same connection to avoid
            # two callers racing on MAX+1.
            async with db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
                "FROM compact_store WHERE session_id = ?",
                (record.session_id,),
            ) as cur:
                row = await cur.fetchone()
            next_version = int(row[0]) if row else 1
            record.version = next_version
            await db.execute(
                "INSERT INTO compact_store ("
                "  session_id, version, source_hash, original_tokens, "
                "  compacted_tokens, original_message_count, "
                "  kept_message_ids, summary, model, trigger_kind, "
                "  outcome, created_at, duration_ms"
                ") VALUES ("
                "  :session_id, :version, :source_hash, :original_tokens, "
                "  :compacted_tokens, :original_message_count, "
                "  :kept_message_ids, :summary, :model, :trigger_kind, "
                "  :outcome, :created_at, :duration_ms"
                ")",
                record.to_row(),
            )
            await db.commit()
        return next_version

    async def list_for_session(
        self,
        session_id: str,
        limit: int = 10,
    ) -> list[CompactRecord]:
        """List the latest compacts for a session, newest first.

        Useful for an operator UI ("show me what compaction did to this
        session") and for the audit log. Capped at ``limit`` rows
        (default 10) to avoid pulling the full history by accident.
        """
        if not self._initialized:
            await self.init()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(
                "SELECT * FROM compact_store "
                "WHERE session_id = ? "
                "ORDER BY version DESC LIMIT ?",
                (session_id, int(limit)),
            ) as cur:
                rows = await cur.fetchall()
        return [CompactRecord.from_row(r) for r in rows]

    async def count(self) -> int:
        """Return the total number of rows in ``compact_store``.

        Cheap (``COUNT(*)`` on an indexed table) — used by tests and
        the Phase 4 observability counter to expose ``store_size``.
        """
        if not self._initialized:
            await self.init()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM compact_store") as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0
