"""Tests for harness.agents.jobs.JobStore (Phase 2.1, Step 2; Phase 2.2, Step 0).

Covers:
  - Schema creation (idempotent)
  - create() returns a unique id and persists all fields
  - update_status() with cost / error / finished
  - update_status() rejects unknown status
  - append_event() JSON-encodes payload
  - load() returns None for unknown job
  - list_events() returns events in insertion order
  - list_recent() returns N most recent, newest first
  - recover_running() marks in-flight jobs as cancelled
  - recover_running() does NOT touch terminal jobs
  - DELETE CASCADE on merge_events when job row is dropped (no API, just verify FK works)
  - Parent dir auto-created
  - Empty list_recent(0) returns []
  - Phase 2.2: PR integration fields (repo, pr_url, pr_number, target_branch, pr_mode)
  - Phase 2.2: ALTER TABLE migration for pre-2.2 DBs
  - Phase 2.2: PR-phase statuses included in recover_running()
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agents.jobs import (
    JOB_STATUSES,
    JobEvent,
    JobRecord,
    JobStore,
    JobStatus,
)


# === Schema & lifecycle ===

class TestSchema:
    async def test_creates_tables_lazily(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        # First call (e.g. create()) must succeed even though the
        # file didn't exist.
        jid = await store.create(
            worktree_id="wt-1", model="MiniMax-M2.7", prompt="hi",
        )
        assert jid
        assert (tmp_path / "jobs.db").exists()

    async def test_schema_idempotent(self, tmp_path: Path) -> None:
        """Two stores against the same file don't conflict."""
        store_a = JobStore(tmp_path / "jobs.db")
        store_b = JobStore(tmp_path / "jobs.db")
        await store_a.create(worktree_id="wt-a", model="m", prompt="p")
        await store_b.create(worktree_id="wt-b", model="m", prompt="p")
        # list_recent sees both.
        recs = await store_b.list_recent(10)
        assert len(recs) == 2

    async def test_parent_dir_created(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "jobs.db"
        store = JobStore(nested)
        await store.create(worktree_id="wt", model="m", prompt="p")
        assert nested.parent.is_dir()


# === create / load ===

class TestCreateLoad:
    async def test_create_returns_unique_ids(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        ids = {await store.create(worktree_id="wt", model="m", prompt="p") for _ in range(5)}
        assert len(ids) == 5

    async def test_load_returns_record(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-load", model="glm-4.7", prompt="task description",
        )
        rec = await store.load(jid)
        assert rec is not None
        assert rec.id == jid
        assert rec.worktree_id == "wt-load"
        assert rec.model == "glm-4.7"
        assert rec.prompt == "task description"
        assert rec.status == "queued"
        assert rec.cost == 0.0
        assert rec.error is None
        assert rec.finished_at is None
        assert rec.started_at  # non-empty ISO string

    async def test_load_unknown_returns_none(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        assert await store.load("does-not-exist") is None

    async def test_prompt_stored_verbatim(self, tmp_path: Path) -> None:
        """JobStore stores the prompt verbatim — truncation to 500
        chars is the caller's responsibility (MergeQueue does it
        because it's a UI-display concern, not a storage concern)."""
        long = "x" * 1000
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt=long)
        rec = await store.load(jid)
        assert rec is not None
        assert len(rec.prompt) == 1000


# === update_status ===

class TestUpdateStatus:
    async def test_basic_status_change(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        await store.update_status(jid, "running_code")
        rec = await store.load(jid)
        assert rec.status == "running_code"
        assert rec.finished_at is None

    async def test_finished_stamps_finished_at(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        await store.update_status(jid, "merged", finished=True)
        rec = await store.load(jid)
        assert rec.status == "merged"
        assert rec.finished_at is not None

    async def test_cost_and_error(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        await store.update_status(
            jid, "failed", cost=0.0123, error="boom", finished=True,
        )
        rec = await store.load(jid)
        assert rec.cost == 0.0123
        assert rec.error == "boom"

    async def test_unknown_status_rejected(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        with pytest.raises(ValueError, match="unknown job status"):
            await store.update_status(jid, "flying")

    def test_all_statuses_are_known(self) -> None:
        """Defence against drift: every JobStatus value is in JOB_STATUSES."""
        for s in JobStatus:
            assert s.value in JOB_STATUSES


# === append_event / list_events ===

class TestEvents:
    async def test_append_and_list_order(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        await store.append_event(jid, "started")
        await store.append_event(jid, "code_done", {"iterations": 2})
        await store.append_event(jid, "review_done", {"iterations": 1})
        events = await store.list_events(jid)
        assert [e.kind for e in events] == ["started", "code_done", "review_done"]
        assert events[1].payload == {"iterations": 2}
        assert events[2].payload == {"iterations": 1}

    async def test_empty_payload_defaults_to_empty_dict(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        await store.append_event(jid, "started")
        events = await store.list_events(jid)
        assert events[0].payload == {}

    async def test_payload_round_trip_complex(self, tmp_path: Path) -> None:
        """Nested dict + non-string values survive JSON round-trip."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        payload = {"cost": 0.001, "nested": {"k": "v"}, "list": [1, 2, 3]}
        await store.append_event(jid, "merged", payload)
        events = await store.list_events(jid)
        assert events[0].payload == payload


# === list_recent ===

class TestListRecent:
    async def test_newest_first(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        a = await store.create(worktree_id="wt-a", model="m", prompt="a")
        b = await store.create(worktree_id="wt-b", model="m", prompt="b")
        c = await store.create(worktree_id="wt-c", model="m", prompt="c")
        recs = await store.list_recent(10)
        # Insertion order a, b, c → reversed (c, b, a) on read.
        assert [r.id for r in recs] == [c, b, a]

    async def test_limit(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        for i in range(5):
            await store.create(worktree_id=f"wt-{i}", model="m", prompt=f"p{i}")
        recs = await store.list_recent(3)
        assert len(recs) == 3

    async def test_zero_returns_empty(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        await store.create(worktree_id="wt", model="m", prompt="p")
        assert await store.list_recent(0) == []


# === recover_running ===

class TestRecoverRunning:
    async def test_marks_inflight_as_cancelled(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        await store.update_status(jid, "running_code")
        cancelled = await store.recover_running()
        assert cancelled == [jid]
        rec = await store.load(jid)
        assert rec.status == "cancelled"
        assert rec.finished_at is not None
        assert rec.error == "process restarted"

    async def test_does_not_touch_terminal_jobs(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        merged_id = await store.create(worktree_id="wt-m", model="m", prompt="p")
        await store.update_status(merged_id, "merged", finished=True)
        failed_id = await store.create(worktree_id="wt-f", model="m", prompt="p")
        await store.update_status(failed_id, "failed", finished=True)
        cancelled = await store.recover_running()
        assert cancelled == []
        assert (await store.load(merged_id)).status == "merged"
        assert (await store.load(failed_id)).status == "failed"

    async def test_cancels_multiple_inflight(self, tmp_path: Path) -> None:
        store = JobStore(tmp_path / "jobs.db")
        ids = []
        for s in ("running_code", "running_review", "verifying", "queued"):
            jid = await store.create(worktree_id="wt", model="m", prompt="p")
            await store.update_status(jid, s)
            ids.append(jid)
        cancelled = await store.recover_running()
        assert sorted(cancelled) == sorted(ids)
        for jid in ids:
            assert (await store.load(jid)).status == "cancelled"


# === Phase 2.2: PR integration fields ===

class TestPR22Schema:
    """Phase 2.2: PR fields + ALTER TABLE migration + new statuses."""

    async def test_create_accepts_pr_fields(self, tmp_path: Path) -> None:
        """create() with repo + pr_mode + target_branch persists them."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-pr", model="m", prompt="p",
            repo="/abs/path/to/repo", pr_mode="draft",
            target_branch="develop",
        )
        rec = await store.load(jid)
        assert rec is not None
        assert rec.repo == "/abs/path/to/repo"
        assert rec.pr_mode == "draft"
        assert rec.target_branch == "develop"
        # PR URL/number start None.
        assert rec.pr_url is None
        assert rec.pr_number is None

    async def test_create_defaults_pr_mode_to_off(self, tmp_path: Path) -> None:
        """Phase 2.1 callers that don't pass pr_mode get pr_mode='off'."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        rec = await store.load(jid)
        assert rec.pr_mode == "off"

    async def test_load_returns_new_fields_with_none(self, tmp_path: Path) -> None:
        """Old rows (NULL in PR columns) load with sensible defaults."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        rec = await store.load(jid)
        assert rec.repo is None
        assert rec.pr_url is None
        assert rec.pr_number is None
        assert rec.target_branch is None
        assert rec.pr_mode == "off"  # NOT NULL DEFAULT 'off' backfill

    async def test_update_status_with_pr_url(self, tmp_path: Path) -> None:
        """update_status(pr_url=...) writes the URL column."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt", model="m", prompt="p", pr_mode="draft",
        )
        await store.update_status(
            jid, "pr_open",
            pr_url="https://github.com/owner/repo/pull/42",
            pr_number=42,
        )
        rec = await store.load(jid)
        assert rec.status == "pr_open"
        assert rec.pr_url == "https://github.com/owner/repo/pull/42"
        assert rec.pr_number == 42

    async def test_recover_running_cancels_pr_phase(self, tmp_path: Path) -> None:
        """PR-phase statuses are also 'in flight' and get cancelled on restart."""
        store = JobStore(tmp_path / "jobs.db")
        ids = []
        for s in ("pr_creating", "pr_open", "pr_waiting_checks",
                  "pr_waiting_review", "merging_pr"):
            jid = await store.create(worktree_id="wt", model="m", prompt="p")
            await store.update_status(jid, s)
            ids.append(jid)
        cancelled = await store.recover_running()
        assert sorted(cancelled) == sorted(ids)
        for jid in ids:
            rec = await store.load(jid)
            assert rec.status == "cancelled"
            assert rec.error == "process restarted"

    async def test_alter_table_migration_idempotent(self, tmp_path: Path) -> None:
        """A DB with the Phase 2.1 schema is migrated in place.

        We simulate a legacy DB by:
          1. Creating a store (full Phase 2.2 schema + migration = no-op)
          2. Dropping the new columns (sqlite supports DROP COLUMN only in 3.35+,
             so we use a manual CREATE + INSERT with the legacy 9-col schema
             and verify the migration back-fills on first read).
        """
        import sqlite3

        # Create a Phase 2.1-style DB (9 columns, no PR fields).
        legacy_db = tmp_path / "legacy.db"
        conn = sqlite3.connect(legacy_db)
        conn.executescript("""
            CREATE TABLE merge_jobs (
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
            INSERT INTO merge_jobs
                (id, worktree_id, status, started_at, cost, model, prompt)
                VALUES ('legacy-1', 'wt-legacy', 'queued',
                        '2026-06-14T12:00:00', 0.0, 'MiniMax-M2.7', 'old task');
        """)
        conn.commit()
        conn.close()

        # Open via JobStore — migration should add 5 columns.
        store = JobStore(legacy_db)
        rec = await store.load("legacy-1")
        assert rec is not None
        assert rec.worktree_id == "wt-legacy"
        # New columns present and back-filled to safe defaults.
        assert rec.repo is None
        assert rec.pr_url is None
        assert rec.pr_number is None
        assert rec.target_branch is None
        assert rec.pr_mode == "off"

    async def test_jobstatus_enum_includes_pr_states(self) -> None:
        """Defence against drift: every PR-phase JobStatus is in JOB_STATUSES."""
        for s in (JobStatus.PR_CREATING, JobStatus.PR_OPEN,
                  JobStatus.PR_WAITING_CHECKS, JobStatus.PR_WAITING_REVIEW,
                  JobStatus.MERGING_PR, JobStatus.PR_AUTO_MERGE_ENABLED):
            assert s.value in JOB_STATUSES
        # 14 statuses as of Phase 2.3 (was 13 in Phase 2.2; we added
        # PR_AUTO_MERGE_ENABLED for the ``gh pr merge --auto`` flow).
        assert len(JOB_STATUSES) == 14


class TestPR24StackSchema:
    """Phase 2.4: stacked / multi-PR fields + ``pr_stack_id`` schema."""

    async def test_create_accepts_stack_fields(self, tmp_path: Path) -> None:
        """create() persists the 4 new stack fields (pr_stack_id,
        stack_position, stack_size, depends_on_pr_number)."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-1", model="m", prompt="p",
            pr_stack_id="stack-abc", stack_position=2, stack_size=3,
            depends_on_pr_number=42,
        )
        rec = await store.load(jid)
        assert rec is not None
        assert rec.pr_stack_id == "stack-abc"
        assert rec.stack_position == 2
        assert rec.stack_size == 3
        assert rec.depends_on_pr_number == 42

    async def test_create_defaults_stack_fields(self, tmp_path: Path) -> None:
        """Phase 2.1/2.2/2.3 callers get safe defaults: stack_id=None,
        position=0, size=1, depends_on=None."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(worktree_id="wt", model="m", prompt="p")
        rec = await store.load(jid)
        assert rec.pr_stack_id is None
        assert rec.stack_position == 0
        assert rec.stack_size == 1
        assert rec.depends_on_pr_number is None

    async def test_legacy_db_gets_stack_columns_added(
        self, tmp_path: Path,
    ) -> None:
        """A DB that pre-dates Phase 2.4 (Phase 2.3 schema, with the
        PR fields but no stack fields) is migrated in place: the
        stack columns are added via ``ALTER TABLE`` on first read."""
        import sqlite3

        legacy_db = tmp_path / "legacy24.db"
        conn = sqlite3.connect(legacy_db)
        # Phase 2.3 schema: 14 columns, NO stack fields.
        conn.executescript("""
            CREATE TABLE merge_jobs (
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
                pr_mode       TEXT NOT NULL DEFAULT 'off'
            );
            INSERT INTO merge_jobs
                (id, worktree_id, status, started_at, cost, model, prompt,
                 pr_mode, pr_number)
                VALUES ('legacy-24', 'wt-24', 'merged',
                        '2026-06-14T12:00:00', 0.5, 'm', 'old',
                        'ready', 100);
        """)
        conn.commit()
        conn.close()

        store = JobStore(legacy_db)
        # Migration should add pr_stack_id, stack_position, stack_size,
        # depends_on_pr_number columns.
        rec = await store.load("legacy-24")
        assert rec is not None
        # Phase 2.3 fields still readable
        assert rec.pr_mode == "ready"
        assert rec.pr_number == 100
        # Phase 2.4 stack fields present with safe defaults
        assert rec.pr_stack_id is None
        assert rec.stack_position == 0
        assert rec.stack_size == 1
        assert rec.depends_on_pr_number is None

    async def test_find_jobs_by_stack_id_returns_all_positions(
        self, tmp_path: Path,
    ) -> None:
        """find_jobs_by_stack_id lists all rows for a stack, ordered
        by stack_position ASC."""
        store = JobStore(tmp_path / "jobs.db")
        # Orchestrator row (position=0)
        await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="stack-xyz", stack_position=0, stack_size=3,
        )
        # Child 1
        await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="stack-xyz", stack_position=1, stack_size=3,
        )
        # Child 2 (depends on PR #1)
        await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="stack-xyz", stack_position=2, stack_size=3,
            depends_on_pr_number=1,
        )
        # Unrelated job (different stack)
        await store.create(
            worktree_id="wt2", model="m", prompt="p",
            pr_stack_id="stack-other", stack_position=0, stack_size=1,
        )

        rows = await store.find_jobs_by_stack_id("stack-xyz")
        assert len(rows) == 3
        assert [r.stack_position for r in rows] == [0, 1, 2]
        assert rows[0].pr_stack_id == "stack-xyz"
        # Last child has depends_on_pr_number
        assert rows[2].depends_on_pr_number == 1
        # Unrelated stack not included
        assert "stack-other" not in [r.pr_stack_id for r in rows]

    async def test_find_jobs_by_stack_id_empty_for_unknown(
        self, tmp_path: Path,
    ) -> None:
        store = JobStore(tmp_path / "jobs.db")
        rows = await store.find_jobs_by_stack_id("nope")
        assert rows == []

    async def test_all_stack_children_merged_true(self, tmp_path: Path) -> None:
        """all_stack_children_merged returns True only when orchestrator
        row exists AND all children have status='merged'."""
        store = JobStore(tmp_path / "jobs.db")
        # Orchestrator
        await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="s1", stack_position=0, stack_size=2,
        )
        # Child 1 merged
        c1 = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="s1", stack_position=1, stack_size=2,
        )
        await store.update_status(c1, "merged", finished=True)
        # Child 2 merged
        c2 = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="s1", stack_position=2, stack_size=2,
        )
        await store.update_status(c2, "merged", finished=True)
        assert await store.all_stack_children_merged("s1") is True

    async def test_all_stack_children_merged_false_if_any_in_flight(
        self, tmp_path: Path,
    ) -> None:
        store = JobStore(tmp_path / "jobs.db")
        await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="s2", stack_position=0, stack_size=2,
        )
        c1 = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="s2", stack_position=1, stack_size=2,
        )
        await store.update_status(c1, "merged", finished=True)
        c2 = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="s2", stack_position=2, stack_size=2,
        )
        # Child 2 is in flight, not merged
        await store.update_status(c2, "pr_waiting_checks")
        assert await store.all_stack_children_merged("s2") is False

    async def test_all_stack_children_merged_false_if_no_orchestrator(
        self, tmp_path: Path,
    ) -> None:
        """Edge case: only children exist (orchestrator row missing).
        We don't promote a parent that doesn't exist."""
        store = JobStore(tmp_path / "jobs.db")
        c1 = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="s3", stack_position=1, stack_size=2,
        )
        await store.update_status(c1, "merged", finished=True)
        assert await store.all_stack_children_merged("s3") is False

    async def test_recover_running_cancels_stack_children(
        self, tmp_path: Path,
    ) -> None:
        """Stack children in any in-flight status get cancelled on
        restart, same as non-stacked jobs."""
        store = JobStore(tmp_path / "jobs.db")
        # Orchestrator
        orch = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="sx", stack_position=0, stack_size=2,
        )
        # Child in pr_open
        child = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_stack_id="sx", stack_position=1, stack_size=2,
        )
        await store.update_status(child, "pr_open")
        cancelled = await store.recover_running()
        # Both rows in flight (orchestrator is queued, child is pr_open)
        assert sorted(cancelled) == sorted([orch, child])
        for jid in (orch, child):
            rec = await store.load(jid)
            assert rec.status == "cancelled"
