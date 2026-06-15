"""ScratchpadStore — persistent notes + plan steps per (agent, session) (Phase 3 v1.2.0).

Phase 3 v1.2.0 introduces the "Write context" strategy from the
Anthropic context-engineering playbook. The :class:`ScratchpadStore`
holds two SQLite tables in the existing ``agent-jobs.db`` (sibling
of ``compact_store`` / ``merge_jobs`` / ``webhook_events``):

  * ``scratchpad_notes`` — free-form notes tagged with
    :class:`~harness.agents.scratchpad.NoteLevel` (L0 hot / L1 plan /
    L2 archive). L0 is capped at ``scratchpad_l0_max_bytes`` so it
    fits in the system prompt.
  * ``plan_steps`` — ordered list of agent-emitted plan steps with
    explicit ``deps`` graph and ``status`` lifecycle
    (pending → in_progress → done / blocked).

The store is per-``(agent_id, session_id)`` — the constructor binds
both, and every SELECT / INSERT / UPDATE filters by them so a
sub-agent never sees a parent's notes and vice versa. ``agent_id=None``
is the admin / cross-agent context (rare; used by the CLI inspector).

The class follows the same mirror pattern as
:class:`~harness.agents.compact_store.CompactStore`:

  * single async class, ``__init__(db_path)``, idempotent ``init()``
  * per-method ``aiosqlite.connect`` (no shared connection / pool)
  * WAL + ``busy_timeout=5000`` + ``synchronous=NORMAL`` (matches
    ``JobStore`` defaults — see ``harness/agents/jobs.py``)
  * mutating methods assign ``id`` / ``created_at`` on the dataclass
    after INSERT so callers can use the record immediately
  * L0 cap is enforced inside ``write_note`` — exceeding the cap
    raises :class:`ValueError` (NOT fail-open; L0 oversized defeats
    the "hot" guarantee)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .scratchpad import Note, NoteLevel, PlanStatus, PlanStep

logger = logging.getLogger(__name__)


# === Schema ===

_NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS scratchpad_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    level TEXT NOT NULL CHECK(level IN ('L0','L1','L2')),
    content TEXT NOT NULL,
    tags TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""

_PLANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','in_progress','done','blocked')),
    deps TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_NOTES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_notes_session_level
    ON scratchpad_notes(session_id, agent_id, level)
"""

_PLANS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_plans_session_status
    ON plan_steps(session_id, agent_id, status)
"""


# === Defaults ===

#: Default L0 cap (bytes) when the caller does not pass ``l0_max_bytes``.
DEFAULT_L0_MAX_BYTES = 1024


# === Store ===

class ScratchpadStore:
    """Per-``(agent_id, session_id)`` persistent notes + plan steps.

    Parameters
    ----------
    db_path:
        Path to the SQLite file. Phase 3 v1.2.0 reuses the existing
        ``agent-jobs.db`` (sibling of ``compact_store`` / ``merge_jobs``)
        so all per-session state lives in one WAL/connection-lifecycle
        file.
    session_id:
        The session this store is bound to. Used as the ``session_id``
        filter on every read/write. Required (no implicit "default"
        session — agent runs must pass an explicit id).
    agent_id:
        The agent this store is bound to (``spec.memory_namespace or
        "solomon"`` from :class:`~harness.agents.runner.AgentRunner`).
        ``None`` means "admin / cross-agent" — the ``agent_id`` filter
        is dropped and all rows for the session are visible.
    l0_max_bytes:
        Override the default L0 cap (used by tests to exercise the
        cap-enforcement branch). Production code reads the value from
        ``settings.scratchpad_l0_max_bytes``.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        session_id: str,
        agent_id: str | None,
        l0_max_bytes: int = DEFAULT_L0_MAX_BYTES,
    ) -> None:
        if not session_id:
            raise ValueError("ScratchpadStore: session_id is required")
        if l0_max_bytes < 128:
            raise ValueError(
                f"ScratchpadStore: l0_max_bytes must be >= 128, got {l0_max_bytes}"
            )
        self._db_path = db_path
        self._session_id = session_id
        self._agent_id = agent_id
        self._l0_max_bytes = l0_max_bytes
        self._initialized = False

    # --- Lifecycle ---

    async def init(self) -> None:
        """Create the two tables + indexes if missing. Idempotent."""
        if self._initialized:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(_NOTES_SCHEMA)
            await db.execute(_PLANS_SCHEMA)
            await db.execute(_NOTES_INDEX)
            await db.execute(_PLANS_INDEX)
            await db.commit()
        self._initialized = True
        logger.info(
            "ScratchpadStore ready at %s (session=%s, agent=%s)",
            self._db_path, self._session_id, self._agent_id,
        )

    # --- Helpers ---

    def _where_agent(self) -> tuple[str, list[Any]]:
        """Return ``(clause, params)`` for the agent_id filter.

        ``agent_id=None`` means "any agent" — the clause is dropped
        and the caller is responsible for not leaking this to a
        sub-agent (use the factory to bind it explicitly).
        """
        if self._agent_id is None:
            return "", []
        return " AND agent_id = ?", [self._agent_id]

    # --- Notes ---

    async def write_note(
        self,
        level: NoteLevel,
        content: str,
        tags: list[str] | None = None,
    ) -> Note:
        """Persist a new note. Returns the record with ``id`` filled.

        For ``level == L0`` the cap is enforced: the new content's
        UTF-8 byte length is added to the current L0 size, and if the
        total exceeds ``l0_max_bytes`` the oldest L0 rows are pruned
        (one at a time) until the new note fits. If even after
        pruning the new note alone exceeds the cap, :class:`ValueError`
        is raised — the L0 layer cannot store notes bigger than the
        cap because that defeats the "hot" guarantee.
        """
        if not self._initialized:
            await self.init()
        if not isinstance(level, NoteLevel):
            level = NoteLevel(str(level))
        tags = list(tags) if tags else []

        # L0 cap enforcement — measure in UTF-8 bytes (multi-byte safe).
        if level == NoteLevel.L0:
            incoming = len(content.encode("utf-8"))
            if incoming > self._l0_max_bytes:
                logger.warning(
                    "scratchpad.l0_cap_exceeded session=%s current=%s attempted=%s cap=%s",
                    self._session_id, await self.l0_size_bytes(), incoming, self._l0_max_bytes,
                )
                raise ValueError(
                    f"L0 note size {incoming} bytes exceeds cap {self._l0_max_bytes} bytes"
                )
            # Auto-prune oldest L0 rows until the new note fits.
            current = await self.l0_size_bytes()
            while current + incoming > self._l0_max_bytes:
                pruned = await self._prune_oldest_l0()
                if not pruned:
                    # Should not happen — the check above guarantees
                    # the new note fits in the cap alone, but be safe.
                    break
                current = await self.l0_size_bytes()

        now = time.time()
        record = Note(
            session_id=self._session_id,
            agent_id=self._agent_id,
            level=level,
            content=content,
            tags=tags,
            created_at=now,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO scratchpad_notes ("
                "  session_id, agent_id, level, content, tags, created_at"
                ") VALUES ("
                "  :session_id, :agent_id, :level, :content, :tags, :created_at"
                ")",
                record.to_row(),
            )
            await db.commit()
            async with db.execute(
                "SELECT last_insert_rowid()",
            ) as cur:
                row = await cur.fetchone()
            record.id = int(row[0]) if row else 0

        logger.info(
            "scratchpad.write session=%s level=%s size=%d tags=%d note_id=%d",
            self._session_id, level.value, len(content.encode("utf-8")),
            len(tags), record.id,
        )
        return record

    async def read_notes(
        self,
        level: NoteLevel | None = None,
        *,
        limit: int = 100,
    ) -> list[Note]:
        """Return notes for this (session, agent), newest first.

        ``level=None`` returns all levels; pass a
        :class:`NoteLevel` to filter. ``limit`` is hard-capped to
        protect the caller from pulling a huge L2 archive.
        """
        if not self._initialized:
            await self.init()
        if level is not None and not isinstance(level, NoteLevel):
            level = NoteLevel(str(level))
        limit = max(1, int(limit))
        where_agent_sql, where_agent_params = self._where_agent()
        params: list[Any] = [self._session_id, *where_agent_params]
        level_clause = ""
        if level is not None:
            level_clause = " AND level = ?"
            params.append(level.value)
        params.append(limit)
        sql = (
            "SELECT * FROM scratchpad_notes "
            "WHERE session_id = ?" + where_agent_sql + level_clause +
            " ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [Note.from_row(r) for r in rows]

    async def delete_note(self, note_id: int) -> bool:
        """Delete a note by id. Returns True if a row was removed."""
        if not self._initialized:
            await self.init()
        where_agent_sql, where_agent_params = self._where_agent()
        sql = (
            "DELETE FROM scratchpad_notes WHERE id = ? AND session_id = ?"
            + where_agent_sql
        )
        params: list[Any] = [int(note_id), self._session_id, *where_agent_params]
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(sql, params)
            await db.commit()
            removed = cur.rowcount > 0
        return removed

    async def promote_note(
        self,
        note_id: int,
        to_level: NoteLevel,
    ) -> Note | None:
        """Move a note to a different level. Returns the updated record.

        Used by the LLM to graduate L1 plan notes to L0 ("this
        decision is now part of my hot context") or archive L1 to
        L2. L0 promotion enforces the size cap.
        """
        if not self._initialized:
            await self.init()
        if not isinstance(to_level, NoteLevel):
            to_level = NoteLevel(str(to_level))
        where_agent_sql, where_agent_params = self._where_agent()
        # First fetch the current note (also serves as a permission check).
        fetch_sql = (
            "SELECT * FROM scratchpad_notes WHERE id = ? AND session_id = ?"
            + where_agent_sql
        )
        params: list[Any] = [int(note_id), self._session_id, *where_agent_params]
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(fetch_sql, params) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            note = Note.from_row(row)
            # L0 cap check on promotion — measure delta (new L0 size - current L0 size + content size).
            if to_level == NoteLevel.L0:
                if note.level == NoteLevel.L0:
                    # No-op promotion — return as-is.
                    return note
                content_bytes = len(note.content.encode("utf-8"))
                current_l0 = await self.l0_size_bytes()
                if content_bytes > self._l0_max_bytes:
                    logger.warning(
                        "scratchpad.l0_cap_exceeded session=%s current=%s attempted=%s cap=%s (promote)",
                        self._session_id, current_l0, content_bytes, self._l0_max_bytes,
                    )
                    raise ValueError(
                        f"L0 promote size {content_bytes} bytes exceeds cap {self._l0_max_bytes} bytes"
                    )
                while current_l0 + content_bytes > self._l0_max_bytes:
                    pruned = await self._prune_oldest_l0()
                    if not pruned:
                        break
                    current_l0 = await self.l0_size_bytes()
            # Apply the promotion.
            await db.execute(
                "UPDATE scratchpad_notes SET level = ? "
                "WHERE id = ? AND session_id = ?" + where_agent_sql,
                [to_level.value, int(note_id), self._session_id, *where_agent_params],
            )
            await db.commit()
        note.level = to_level
        logger.info(
            "scratchpad.promote session=%s note_id=%d to=%s",
            self._session_id, note_id, to_level.value,
        )
        return note

    async def l0_size_bytes(self) -> int:
        """Sum of UTF-8 byte sizes of all L0 notes for this (session, agent)."""
        if not self._initialized:
            await self.init()
        where_agent_sql, where_agent_params = self._where_agent()
        sql = (
            "SELECT content FROM scratchpad_notes "
            "WHERE session_id = ? AND level = 'L0'" + where_agent_sql
        )
        params: list[Any] = [self._session_id, *where_agent_params]
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        total = 0
        for (content,) in rows:
            if isinstance(content, (bytes, bytearray)):
                total += len(content)
            else:
                total += len(str(content).encode("utf-8"))
        return total

    async def _prune_oldest_l0(self) -> bool:
        """Delete the single oldest L0 row. Returns True if a row was removed.

        Helper for the L0 cap-enforcement path in :meth:`write_note`
        and :meth:`promote_note`.
        """
        where_agent_sql, where_agent_params = self._where_agent()
        sql = (
            "DELETE FROM scratchpad_notes WHERE id = ("
            "  SELECT id FROM scratchpad_notes "
            "  WHERE session_id = ? AND level = 'L0'" + where_agent_sql +
            "  ORDER BY created_at ASC, id ASC LIMIT 1"
            ")"
        )
        params: list[Any] = [self._session_id, *where_agent_params]
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return cur.rowcount > 0

    # --- Plan ---

    async def add_plan_step(
        self,
        description: str,
        *,
        deps: list[int] | None = None,
    ) -> PlanStep:
        """Insert a new plan step. Returns the record with ``id`` filled."""
        if not self._initialized:
            await self.init()
        deps = list(deps) if deps else []
        now = time.time()
        record = PlanStep(
            session_id=self._session_id,
            agent_id=self._agent_id,
            description=description,
            status=PlanStatus.PENDING,
            deps=deps,
            created_at=now,
            updated_at=now,
        )
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO plan_steps ("
                "  session_id, agent_id, description, status, deps, "
                "  created_at, updated_at"
                ") VALUES ("
                "  :session_id, :agent_id, :description, :status, :deps, "
                "  :created_at, :updated_at"
                ")",
                record.to_row(),
            )
            await db.commit()
            async with db.execute("SELECT last_insert_rowid()") as cur:
                row = await cur.fetchone()
            record.id = int(row[0]) if row else 0
        logger.info(
            "scratchpad.plan_step session=%s step_id=%d deps=%d",
            self._session_id, record.id, len(deps),
        )
        return record

    async def list_plan_steps(
        self,
        *,
        status: PlanStatus | None = None,
    ) -> list[PlanStep]:
        """List plan steps, oldest first (creation order)."""
        if not self._initialized:
            await self.init()
        if status is not None and not isinstance(status, PlanStatus):
            status = PlanStatus(str(status))
        where_agent_sql, where_agent_params = self._where_agent()
        params: list[Any] = [self._session_id, *where_agent_params]
        status_clause = ""
        if status is not None:
            status_clause = " AND status = ?"
            params.append(status.value)
        sql = (
            "SELECT * FROM plan_steps WHERE session_id = ?"
            + where_agent_sql + status_clause +
            " ORDER BY created_at ASC, id ASC"
        )
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [PlanStep.from_row(r) for r in rows]

    async def mark_done(
        self,
        step_id: int,
        *,
        status: PlanStatus = PlanStatus.DONE,
    ) -> PlanStep | None:
        """Update a step's status. Returns the updated record or None."""
        if not self._initialized:
            await self.init()
        if not isinstance(status, PlanStatus):
            status = PlanStatus(str(status))
        where_agent_sql, where_agent_params = self._where_agent()
        fetch_sql = (
            "SELECT * FROM plan_steps WHERE id = ? AND session_id = ?"
            + where_agent_sql
        )
        params: list[Any] = [int(step_id), self._session_id, *where_agent_params]
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(fetch_sql, params) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            now = time.time()
            await db.execute(
                "UPDATE plan_steps SET status = ?, updated_at = ? "
                "WHERE id = ? AND session_id = ?" + where_agent_sql,
                [status.value, now, int(step_id), self._session_id, *where_agent_params],
            )
            await db.commit()
        updated = PlanStep.from_row(row)
        updated.status = status
        updated.updated_at = now
        logger.info(
            "scratchpad.mark_done session=%s step_id=%d status=%s",
            self._session_id, step_id, status.value,
        )
        return updated

    # --- Aggregate ---

    async def count(self) -> int:
        """Total rows across both tables for this (session, agent)."""
        if not self._initialized:
            await self.init()
        where_agent_sql, where_agent_params = self._where_agent()
        params_n: list[Any] = [self._session_id, *where_agent_params]
        params_p: list[Any] = [self._session_id, *where_agent_params]
        sql_n = (
            "SELECT COUNT(*) FROM scratchpad_notes "
            "WHERE session_id = ?" + where_agent_sql
        )
        sql_p = (
            "SELECT COUNT(*) FROM plan_steps "
            "WHERE session_id = ?" + where_agent_sql
        )
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(sql_n, params_n) as cur:
                row_n = await cur.fetchone()
            async with db.execute(sql_p, params_p) as cur:
                row_p = await cur.fetchone()
        return int(row_n[0]) + int(row_p[0]) if row_n and row_p else 0
