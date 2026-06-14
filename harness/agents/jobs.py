"""JobStore — persistent storage for background merge-queue jobs (Phase 2.1, Step 2).

The merge queue's :meth:`~harness.agents.merge_queue.MergeQueue.enqueue_async`
returns a ``job_id`` immediately and runs the job in a background
``asyncio.Task``. The :class:`JobStore` keeps the job's status and event
log in a small SQLite table so:

  1. The CLI / Web UI can ``GET /api/v1/agents/jobs/<id>`` and get a
     fresh status even if the process restarted.
  2. ``recover_running()`` can find jobs that were in flight when the
     process died and mark them as ``cancelled`` (so a human can decide
     whether to re-enqueue).

The schema is intentionally separate from the main harness session
schema (``harness.server.db.sqlite``) so :mod:`harness.agents` does not
import from :mod:`harness.server` — preserving the trust boundary
established in Phase 2.0.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

logger = logging.getLogger(__name__)


# === Constants ===

#: All known job status values. Stored as TEXT in SQLite.
JOB_STATUSES: tuple[str, ...] = (
    "queued",
    "running_code",
    "running_review",
    "verifying",
    "merged",
    "failed",
    "timeout",
    "cancelled",
)

#: Statuses that mean the job is still in flight. ``recover_running``
#: uses this set to find jobs that were active when the process died.
_RUNNING_STATUSES: frozenset[str] = frozenset({
    "queued", "running_code", "running_review", "verifying",
})


# === Schemas ===

class JobStatus(str, Enum):
    """Job lifecycle states. The string values match the SQLite column."""

    QUEUED = "queued"
    RUNNING_CODE = "running_code"
    RUNNING_REVIEW = "running_review"
    VERIFYING = "verifying"
    MERGED = "merged"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobRecord:
    """A row in the ``merge_jobs`` table.

    Immutable. Construct via :meth:`JobStore.load` (preferred) or
    directly in tests.
    """

    id: str
    worktree_id: str
    status: str
    started_at: str           # ISO-8601 naive UTC
    finished_at: str | None
    cost: float
    error: str | None
    model: str
    prompt: str               # included for ``list_recent`` UI display


@dataclass(frozen=True)
class JobEvent:
    """One entry in a job's event log (the ``merge_events`` table)."""

    id: int
    job_id: str
    ts: str
    kind: str                 # "started" | "code_done" | "review_done" | ...
    payload: dict[str, Any] = field(default_factory=dict)


# === SQLite schema ===

#: ``merge_jobs`` — one row per enqueued job.
#: ``merge_events`` — append-only event log per job (used for ``subscribe``
#: replay and for debugging after a crash).
SCHEMA: str = """
CREATE TABLE IF NOT EXISTS merge_jobs (
    id          TEXT PRIMARY KEY,
    worktree_id TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    cost        REAL NOT NULL DEFAULT 0.0,
    error       TEXT,
    model       TEXT NOT NULL,
    prompt      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merge_jobs_status ON merge_jobs(status);
CREATE INDEX IF NOT EXISTS idx_merge_jobs_started ON merge_jobs(started_at DESC);

CREATE TABLE IF NOT EXISTS merge_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES merge_jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_merge_events_job ON merge_events(job_id, id);
"""


# === Helpers ===

def _utcnow() -> datetime:
    """UTC now without tzinfo (matches the main sqlite.py convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _new_job_id() -> str:
    """Random short id — used as the primary key."""
    return uuid4().hex[:16]


# === Store ===

class JobStore:
    """Async SQLite store for merge-queue jobs.

    The store is intentionally tiny — three CRUD operations and one
    recovery scan. Tables are created lazily on first use so the
    store works against a fresh DB file (e.g. in tests).

    Args:
        db_path: Path to the SQLite file. Parent directories are
            created if they don't exist.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        # ``_initialized`` is module-level state per-path; we use a
        # per-instance flag because the test suite swaps the path.
        self._initialized: bool = False

    async def _ensure_schema(self) -> None:
        """Create tables on first connect. Cheap; idempotent."""
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        self._initialized = True

    # --- create / update ---

    async def create(
        self,
        *,
        worktree_id: str,
        model: str,
        prompt: str,
        status: str = "queued",
    ) -> str:
        """Insert a new job row and return its id.

        The id is auto-generated. ``started_at`` is set to now UTC.
        """
        await self._ensure_schema()
        job_id = _new_job_id()
        now = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO merge_jobs
                    (id, worktree_id, status, started_at, cost, model, prompt)
                VALUES (?, ?, ?, ?, 0.0, ?, ?)
                """,
                (job_id, worktree_id, status, now, model, prompt),
            )
            await db.commit()
        return job_id

    async def update_status(
        self,
        job_id: str,
        status: str,
        *,
        cost: float | None = None,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        """Update the job's status (and optionally cost / error / finish).

        Args:
            job_id:    Job id.
            status:    New status. Must be one of :data:`JOB_STATUSES`.
            cost:      If supplied, overwrite the cost column.
            error:     If supplied, overwrite the error column.
            finished:  If True, stamp ``finished_at = now``. Should
                       be True for terminal states (merged/failed/
                       timeout/cancelled). We do NOT validate the
                       status here — the caller is the source of
                       truth on lifecycle.
        """
        if status not in JOB_STATUSES:
            raise ValueError(
                f"unknown job status {status!r}; must be one of {JOB_STATUSES}"
            )
        await self._ensure_schema()
        sets = ["status = ?"]
        args: list[Any] = [status]
        if cost is not None:
            sets.append("cost = ?")
            args.append(float(cost))
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        if finished:
            sets.append("finished_at = ?")
            args.append(_utcnow().isoformat())
        args.append(job_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE merge_jobs SET {', '.join(sets)} WHERE id = ?",
                args,
            )
            await db.commit()

    async def append_event(
        self,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one event to the job's log.

        ``payload`` is JSON-encoded. Use ``subscribe()`` to read the
        events back in order.
        """
        await self._ensure_schema()
        now = _utcnow().isoformat()
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO merge_events (job_id, ts, kind, payload)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, now, kind, body),
            )
            await db.commit()

    # --- read ---

    async def load(self, job_id: str) -> JobRecord | None:
        """Fetch a single job by id. Returns ``None`` if not found."""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, worktree_id, status, started_at, finished_at,
                       cost, error, model, prompt
                FROM merge_jobs WHERE id = ?
                """,
                (job_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return JobRecord(
            id=row["id"], worktree_id=row["worktree_id"],
            status=row["status"], started_at=row["started_at"],
            finished_at=row["finished_at"], cost=row["cost"],
            error=row["error"], model=row["model"], prompt=row["prompt"],
        )

    async def list_events(self, job_id: str) -> list[JobEvent]:
        """All events for a job, oldest first."""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, job_id, ts, kind, payload
                FROM merge_events WHERE job_id = ? ORDER BY id ASC
                """,
                (job_id,),
            ) as cur:
                rows = await cur.fetchall()
        out: list[JobEvent] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else {}
            except json.JSONDecodeError:
                payload = {"_raw": r["payload"]}
            out.append(JobEvent(
                id=r["id"], job_id=r["job_id"], ts=r["ts"],
                kind=r["kind"], payload=payload,
            ))
        return out

    async def list_recent(self, n: int = 20) -> list[JobRecord]:
        """List the ``n`` most recent jobs (newest first)."""
        await self._ensure_schema()
        if n <= 0:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, worktree_id, status, started_at, finished_at,
                       cost, error, model, prompt
                FROM merge_jobs ORDER BY started_at DESC LIMIT ?
                """,
                (int(n),),
            ) as cur:
                rows = await cur.fetchall()
        return [
            JobRecord(
                id=r["id"], worktree_id=r["worktree_id"],
                status=r["status"], started_at=r["started_at"],
                finished_at=r["finished_at"], cost=r["cost"],
                error=r["error"], model=r["model"], prompt=r["prompt"],
            )
            for r in rows
        ]

    # --- recovery ---

    async def recover_running(self) -> list[str]:
        """Mark in-flight jobs (status ∈ ``running_*``) as ``cancelled``.

        Called at process startup. Returns the list of job ids that
        were cancelled. Operators can re-enqueue manually.

        The transition is logged at INFO. The events table is left
        intact (the historical event log is the audit trail).
        """
        await self._ensure_schema()
        cancelled: list[str] = []
        async with aiosqlite.connect(self.db_path) as db:
            placeholders = ",".join("?" for _ in _RUNNING_STATUSES)
            async with db.execute(
                f"SELECT id FROM merge_jobs WHERE status IN ({placeholders})",
                tuple(_RUNNING_STATUSES),
            ) as cur:
                rows = await cur.fetchall()
            for (job_id,) in rows:
                await db.execute(
                    """
                    UPDATE merge_jobs
                    SET status = 'cancelled', finished_at = ?, error = ?
                    WHERE id = ?
                    """,
                    (_utcnow().isoformat(), "process restarted", job_id),
                )
                cancelled.append(job_id)
            await db.commit()
        if cancelled:
            logger.info(
                "JobStore.recover_running: cancelled %d in-flight job(s): %s",
                len(cancelled), cancelled,
            )
        return cancelled


__all__ = [
    "JOB_STATUSES",
    "JobStatus",
    "JobRecord",
    "JobEvent",
    "JobStore",
]
