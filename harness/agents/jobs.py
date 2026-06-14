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
#:
#: Phase 2.2 adds 5 PR-lifecycle statuses (``pr_creating``, ``pr_open``,
#: ``pr_waiting_checks``, ``pr_waiting_review``, ``merging_pr``) for
#: jobs that opt into ``pr_mode="draft"`` or ``pr_mode="ready"``. They
#: are terminal at the queue level (the job IS merged locally or
#: waiting for a human to merge) but in-flight at the GitHub level.
#: Phase 2.3 adds ``pr_auto_merge_enabled`` for jobs that called
#: ``gh pr merge --auto`` (branch-protection-aware) and are waiting
#: for an inbound webhook to mark them ``merged``.
JOB_STATUSES: tuple[str, ...] = (
    "queued",
    "running_code",
    "running_review",
    "verifying",
    "pr_creating",
    "pr_open",
    "pr_waiting_checks",
    "pr_waiting_review",
    "merging_pr",
    "pr_auto_merge_enabled",
    "merged",
    "failed",
    "timeout",
    "cancelled",
)

#: Statuses that mean the job is still in flight. ``recover_running``
#: uses this set to find jobs that were active when the process died.
#: Phase 2.2: include the PR-phase statuses — a job waiting for CI
#: checks is still in flight (a crash should mark it cancelled, not
#: orphan the PR).
#: Phase 2.3: include ``pr_auto_merge_enabled`` — a job waiting for
#: GitHub's branch-protection conditions to clear is still in flight
#: (a crash should mark it cancelled, not orphan the PR).
_RUNNING_STATUSES: frozenset[str] = frozenset({
    "queued", "running_code", "running_review", "verifying",
    "pr_creating", "pr_open", "pr_waiting_checks", "pr_waiting_review",
    "merging_pr", "pr_auto_merge_enabled",
})


# === Schemas ===

class JobStatus(str, Enum):
    """Job lifecycle states. The string values match the SQLite column."""

    QUEUED = "queued"
    RUNNING_CODE = "running_code"
    RUNNING_REVIEW = "running_review"
    VERIFYING = "verifying"
    PR_CREATING = "pr_creating"
    PR_OPEN = "pr_open"
    PR_WAITING_CHECKS = "pr_waiting_checks"
    PR_WAITING_REVIEW = "pr_waiting_review"
    MERGING_PR = "merging_pr"
    #: Phase 2.3: ``gh pr merge --auto`` succeeded; the job is now
    #: waiting for GitHub's branch-protection conditions to clear
    #: (e.g. an outstanding approval). The actual ``merged`` transition
    #: is delivered via the inbound webhook (see
    #: :mod:`harness.agents.webhook_handler`).
    PR_AUTO_MERGE_ENABLED = "pr_auto_merge_enabled"
    MERGED = "merged"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobRecord:
    """A row in the ``merge_jobs`` table.

    Immutable. Construct via :meth:`JobStore.load` (preferred) or
    directly in tests.

    Phase 2.2 adds 5 optional fields for GitHub PR integration. They
    are populated only when the job was enqueued with ``pr_mode != "off"``
    (i.e. ``"draft"`` or ``"ready"``). For Phase 2.1 jobs (no PR
    integration) the fields are ``None`` / ``"off"``.
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
    # === Phase 2.2: PR integration fields ===
    #: Absolute path of the repo the job ran in (per-job override
    #: or ``settings.project_root`` for the default queue). Used by
    #: the cross-repo lock registry and by ``gh pr create``.
    repo: str | None = None
    #: PR URL (e.g. ``https://github.com/owner/repo/pull/12``). Set
    #: after the PR is opened; ``None`` for local-only merges.
    pr_url: str | None = None
    #: PR number extracted from the URL. ``None`` if no PR yet.
    pr_number: int | None = None
    #: Target branch the PR was opened against (defaults to
    #: ``settings.pr_default_target_branch`` = ``"main"``).
    target_branch: str | None = None
    #: PR mode this job was enqueued with: ``"off"`` (default,
    #: local ff-merge only), ``"draft"`` (open draft PR), or
    #: ``"ready"`` (open ready-for-review PR).
    pr_mode: str = "off"
    # === Phase 2.4: stacked / multi-PR fields ===
    #: Stack identifier (``None`` for non-stacked jobs). All jobs
    #: belonging to the same stack share this value (typically a
    #: random hex). The parent orchestrator row uses
    #: ``stack_position=0``; children use ``stack_position >= 1``.
    pr_stack_id: str | None = None
    #: 0-based position within the stack. Position 0 is the
    #: orchestrator row (no ``pr_number``). Positions >= 1 are
    #: individual slice PRs.
    stack_position: int = 0
    #: Total number of slices in the stack (1 for non-stacked jobs).
    #: Only meaningful when ``pr_stack_id`` is set.
    stack_size: int = 1
    #: For stacked jobs: the ``pr_number`` of the previous slice
    #: (``None`` for the first slice; ``None`` for the orchestrator
    #: row). The first slice's PR becomes the base branch for the
    #: second slice's PR, etc.
    depends_on_pr_number: int | None = None


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
#:
#: Phase 2.2 adds 5 columns to ``merge_jobs``:
#:   ``repo TEXT`` — absolute path of the repo the job ran in
#:   ``pr_url TEXT`` — populated after the PR is opened
#:   ``pr_number INTEGER`` — populated after the PR is opened
#:   ``target_branch TEXT`` — ``"main"`` / ``"develop"`` / etc.
#:   ``pr_mode TEXT NOT NULL DEFAULT 'off'`` — ``"off" | "draft" | "ready"``
#:
#: Phase 2.3 adds a sibling table ``webhook_events`` for inbound
#: GitHub webhook idempotency tracking. See
#: :mod:`harness.agents.webhook_store` for the wrapper class. The
#: ``UNIQUE(delivery_id)`` constraint makes ``record_event`` a no-op
#: on redelivery.
#:
#: For DBs that pre-date 2.2, the columns are added via idempotent
#: ``ALTER TABLE ... ADD COLUMN`` migrations in :meth:`JobStore._ensure_schema`
#: (guarded by ``PRAGMA table_info`` so it's safe to run on fresh DBs
#: that already have the columns from ``CREATE TABLE``).
SCHEMA: str = """
CREATE TABLE IF NOT EXISTS merge_jobs (
    id            TEXT PRIMARY KEY,
    worktree_id   TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    cost          REAL NOT NULL DEFAULT 0.0,
    error         TEXT,
    model         TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    repo          TEXT,
    pr_url        TEXT,
    pr_number     INTEGER,
    target_branch TEXT,
    pr_mode       TEXT NOT NULL DEFAULT 'off',
    pr_stack_id   TEXT,
    stack_position INTEGER NOT NULL DEFAULT 0,
    stack_size    INTEGER NOT NULL DEFAULT 1,
    depends_on_pr_number INTEGER
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

CREATE TABLE IF NOT EXISTS webhook_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id   TEXT NOT NULL UNIQUE,
    event_type    TEXT NOT NULL,
    action        TEXT,
    received_at   TEXT NOT NULL,
    processed     INTEGER NOT NULL DEFAULT 0,
    payload       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_webhook_events_delivery ON webhook_events(delivery_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_processed ON webhook_events(processed, received_at);
"""

#: ALTER TABLE statements applied on existing DBs that pre-date
#: Phase 2.2 / 2.4. Each is idempotent (we check ``PRAGMA table_info``
#: before issuing). Order matters: ``ALTER TABLE`` is more lenient
#: in older SQLite versions when columns are added one at a time.
#:
#: Phase 2.2 added 5 columns: ``repo``, ``pr_url``, ``pr_number``,
#: ``target_branch``, ``pr_mode``.
#: Phase 2.4 added 4 columns: ``pr_stack_id``, ``stack_position``,
#: ``stack_size``, ``depends_on_pr_number``.
#: (All 9 columns have safe defaults; legacy rows read back with
#: ``pr_mode='off'``, ``stack_position=0``, ``stack_size=1``.)
_PR24_ALTER_COLUMNS: tuple[tuple[str, str], ...] = (
    # Phase 2.2
    ("repo", "TEXT"),
    ("pr_url", "TEXT"),
    ("pr_number", "INTEGER"),
    ("target_branch", "TEXT"),
    # ``pr_mode`` is NOT NULL DEFAULT 'off' in the CREATE; for
    # ALTER-added columns SQLite applies the DEFAULT to existing
    # rows, so legacy Phase 2.1 rows read back as "off" (back-compat).
    ("pr_mode", "TEXT NOT NULL DEFAULT 'off'"),
    # Phase 2.4
    ("pr_stack_id", "TEXT"),
    ("stack_position", "INTEGER NOT NULL DEFAULT 0"),
    ("stack_size", "INTEGER NOT NULL DEFAULT 1"),
    ("depends_on_pr_number", "INTEGER"),
)


# === Helpers ===

def _utcnow() -> datetime:
    """UTC now without tzinfo (matches the main sqlite.py convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _new_job_id() -> str:
    """Random short id — used as the primary key."""
    return uuid4().hex[:16]


def _row_to_record(row: aiosqlite.Row) -> JobRecord:
    """Map a SELECT row to a :class:`JobRecord`.

    Used by ``load``, ``find_job_by_pr_number``, ``find_jobs_by_stack_id``,
    and ``list_recent`` so the column list and field mapping are defined
    once. Defensive defaults: ``pr_mode`` falls back to ``"off"``,
    ``stack_position`` to ``0``, ``stack_size`` to ``1`` (legacy rows
    from pre-Phase-2.4 DBs that pre-date the stack columns read back
    with these safe defaults).
    """
    return JobRecord(
        id=row["id"],
        worktree_id=row["worktree_id"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cost=row["cost"],
        error=row["error"],
        model=row["model"],
        prompt=row["prompt"],
        repo=row["repo"],
        pr_url=row["pr_url"],
        pr_number=row["pr_number"],
        target_branch=row["target_branch"],
        pr_mode=row["pr_mode"] or "off",
        pr_stack_id=row["pr_stack_id"] if "pr_stack_id" in row.keys() else None,
        stack_position=(
            row["stack_position"] if "stack_position" in row.keys() else 0
        ) or 0,
        stack_size=(
            row["stack_size"] if "stack_size" in row.keys() else 1
        ) or 1,
        depends_on_pr_number=(
            row["depends_on_pr_number"]
            if "depends_on_pr_number" in row.keys() else None
        ),
    )


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
        """Create tables on first connect. Cheap; idempotent.

        Phase 2.2: also applies ``ALTER TABLE`` migrations for
        pre-2.2 databases. We check ``PRAGMA table_info`` for each
        new column and only ``ADD COLUMN`` if it's missing — this
        makes the migration safe to run on both fresh and existing
        DBs (e.g. one created by a Phase 2.1 build).
        """
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await self._apply_phase22_migrations(db)
            await db.commit()
        self._initialized = True

    async def _apply_phase22_migrations(self, db: aiosqlite.Connection) -> None:
        """Add Phase 2.2+ columns + Phase 2.3/2.4 indexes to a pre-existing
        ``merge_jobs`` table.

        For each column in :data:`_PR24_ALTER_COLUMNS`, check
        ``PRAGMA table_info(merge_jobs)`` and issue an ``ALTER TABLE``
        only if the column isn't already present. This is the
        standard SQLite pattern (the ``ADD COLUMN`` itself is not
        idempotent — the ``IF NOT EXISTS`` form is only supported in
        SQLite 3.35+ and we want to support older versions too).

        Phase 2.3: also creates ``idx_merge_jobs_pr_number`` AFTER
        the columns are added. We can't put this index in
        :data:`SCHEMA` because on a legacy (pre-2.2) DB, the column
        doesn't exist yet and the index creation would fail. The
        index is for the Phase 2.3 webhook handler's
        ``find_job_by_pr_number`` lookup.

        Phase 2.4: also creates ``idx_merge_jobs_stack_id`` AFTER
        the stack columns are added. Same rationale — the index
        must follow the column additions.
        """
        async with db.execute("PRAGMA table_info(merge_jobs)") as cur:
            existing = {row[1] async for row in cur}
        for col_name, col_type in _PR24_ALTER_COLUMNS:
            if col_name in existing:
                continue
            # SQLite's ALTER TABLE ADD COLUMN with NOT NULL requires
            # a non-NULL DEFAULT. ``pr_mode`` has DEFAULT 'off' inline,
            # so this is safe.
            await db.execute(
                f"ALTER TABLE merge_jobs ADD COLUMN {col_name} {col_type}"
            )
        # Phase 2.3: index on pr_number for the webhook handler's
        # ``find_job_by_pr_number`` lookup. Idempotent — IF NOT EXISTS
        # means subsequent runs are a no-op.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_merge_jobs_pr_number "
            "ON merge_jobs(pr_number)"
        )
        # Phase 2.4: index on pr_stack_id for the stack orchestrator's
        # ``find_jobs_by_stack_id`` lookup. Idempotent.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_merge_jobs_stack_id "
            "ON merge_jobs(pr_stack_id)"
        )

    # --- create / update ---

    async def create(
        self,
        *,
        worktree_id: str,
        model: str,
        prompt: str,
        status: str = "queued",
        repo: str | None = None,
        pr_mode: str = "off",
        target_branch: str | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        pr_stack_id: str | None = None,
        stack_position: int = 0,
        stack_size: int = 1,
        depends_on_pr_number: int | None = None,
    ) -> str:
        """Insert a new job row and return its id.

        The id is auto-generated. ``started_at`` is set to now UTC.

        Phase 2.2: accepts ``repo``, ``pr_mode``, ``target_branch`` for
        jobs that opt into GitHub PR integration. All three default to
        ``None`` / ``"off"`` for backward compat with Phase 2.1 callers.

        Phase 2.4: accepts ``pr_url``, ``pr_number`` (used by the
        stack orchestrator to persist a child slice's PR at the
        moment of ``create_pr``), plus ``pr_stack_id``,
        ``stack_position``, ``stack_size``, ``depends_on_pr_number``
        for stacked / multi-PR jobs. All default to ``None``/``0``/
        ``1`` for backward compat with Phase 2.1/2.2/2.3 callers.
        """
        await self._ensure_schema()
        job_id = _new_job_id()
        now = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO merge_jobs
                    (id, worktree_id, status, started_at, cost, model,
                     prompt, repo, pr_mode, target_branch,
                     pr_url, pr_number,
                     pr_stack_id, stack_position, stack_size,
                     depends_on_pr_number)
                VALUES (?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?,
                        ?, ?,
                        ?, ?, ?, ?)
                """,
                (job_id, worktree_id, status, now, model, prompt,
                 repo, pr_mode, target_branch,
                 pr_url, pr_number,
                 pr_stack_id, stack_position, stack_size,
                 depends_on_pr_number),
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
        pr_url: str | None = None,
        pr_number: int | None = None,
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
            pr_url:    Phase 2.2: if supplied, set ``pr_url`` (called
                       after ``gh pr create`` succeeds).
            pr_number: Phase 2.2: if supplied, set ``pr_number``.

        Note: ``pr_url`` and ``pr_number`` are normally set together.
        We accept them as independent kwargs so the caller can update
        one without the other (e.g. status flips first, then PR opens).
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
        if pr_url is not None:
            sets.append("pr_url = ?")
            args.append(pr_url)
        if pr_number is not None:
            sets.append("pr_number = ?")
            args.append(int(pr_number))
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
                       cost, error, model, prompt,
                       repo, pr_url, pr_number, target_branch, pr_mode,
                       pr_stack_id, stack_position, stack_size,
                       depends_on_pr_number
                FROM merge_jobs WHERE id = ?
                """,
                (job_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def find_job_by_pr_number(self, pr_number: int) -> JobRecord | None:
        """Look up a job by its ``pr_number`` column.

        Phase 2.3: used by the inbound webhook handler to dispatch
        ``pull_request`` and ``check_run`` events to the originating
        job. There should be at most ONE active job per ``pr_number``
        (the queue holds a per-repo lock; an enqueued job holds the
        PR until the webhook arrives), so we return the most recent
        match. Returns ``None`` if no job has this ``pr_number`` —
        webhooks can fire for PRs that the queue did not create (e.g.
        a human-opened PR), and the handler should ignore those.

        Phase 2.4: only returns jobs with ``pr_number IS NOT NULL``
        (the stack orchestrator row at ``stack_position=0`` has
        ``pr_number=NULL`` and is intentionally excluded — it's
        a coordinator, not a real PR).
        """
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, worktree_id, status, started_at, finished_at,
                       cost, error, model, prompt,
                       repo, pr_url, pr_number, target_branch, pr_mode,
                       pr_stack_id, stack_position, stack_size,
                       depends_on_pr_number
                FROM merge_jobs
                WHERE pr_number = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (int(pr_number),),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def find_jobs_by_stack_id(
        self, stack_id: str,
    ) -> list[JobRecord]:
        """Return all jobs belonging to a stack, ordered by position.

        Phase 2.4: returns rows with ``pr_stack_id = stack_id``,
        ordered by ``stack_position ASC``. The first row
        (``position=0``) is the orchestrator row (no ``pr_number``);
        the rest are child PRs. Returns ``[]`` if the stack_id
        doesn't exist (e.g. a typo, or the stack was deleted).

        This is the inverse of :meth:`find_job_by_pr_number`: instead
        of "PR → job", it answers "stack → all jobs". Used by:
          - ``_run_stack_phase`` to list children
          - ``GET /stacks/{stack_id}`` API endpoint
          - webhook dispatcher to fan-out child-PR events to siblings
        """
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, worktree_id, status, started_at, finished_at,
                       cost, error, model, prompt,
                       repo, pr_url, pr_number, target_branch, pr_mode,
                       pr_stack_id, stack_position, stack_size,
                       depends_on_pr_number
                FROM merge_jobs
                WHERE pr_stack_id = ?
                ORDER BY stack_position ASC, started_at ASC
                """,
                (stack_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def all_stack_children_merged(self, stack_id: str) -> bool:
        """Check if every child PR in a stack is ``merged``.

        Phase 2.4: used by the webhook dispatcher to promote the
        parent orchestrator row to ``merged`` after the last child
        PR is closed+merged. Returns ``True`` only when ALL of:

          - At least one row exists for ``stack_id``
          - The orchestrator row (``stack_position=0``) exists
          - At least one child (``stack_position>=1``) exists
          - Every child row (``stack_position>=1``) has
            ``status='merged'``

        The orchestrator row's status is NOT counted — it has no
        PR and waits for its children to drive the merge state.
        Returns ``False`` otherwise (including: stack doesn't exist,
        no children yet, any child is non-merged, any child is
        in-flight like ``pr_waiting_checks``).
        """
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    SUM(CASE WHEN stack_position = 0 THEN 1 ELSE 0 END)
                        AS orch_count,
                    SUM(CASE WHEN stack_position >= 1 THEN 1 ELSE 0 END)
                        AS child_count,
                    SUM(CASE WHEN stack_position >= 1
                             AND status = 'merged' THEN 1 ELSE 0 END)
                        AS child_merged
                FROM merge_jobs
                WHERE pr_stack_id = ?
                """,
                (stack_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return False
        orch_count = row["orch_count"] or 0
        child_count = row["child_count"] or 0
        child_merged = row["child_merged"] or 0
        # Need: orchestrator exists, at least one child, ALL children
        # are merged (orchestrator status doesn't matter).
        return (
            orch_count >= 1
            and child_count >= 1
            and child_merged == child_count
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
                       cost, error, model, prompt,
                       repo, pr_url, pr_number, target_branch, pr_mode,
                       pr_stack_id, stack_position, stack_size,
                       depends_on_pr_number
                FROM merge_jobs ORDER BY started_at DESC LIMIT ?
                """,
                (int(n),),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

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
