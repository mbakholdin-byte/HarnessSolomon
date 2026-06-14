"""Tests for the Phase 2.3 ``WebhookEventStore`` (idempotency layer).

Covers:
  - ``init()`` creates the ``webhook_events`` table (idempotent)
  - ``record_event()`` returns the row id and persists the payload
  - Duplicate ``delivery_id`` → returns ``None`` (idempotency)
  - ``is_duplicate()`` fast path (True без INSERT)
  - ``mark_processed()`` flips the flag
  - ``count_unprocessed()`` / ``count_total()`` for ops
  - Schema migration: pre-existing ``merge_jobs`` (Phase 2.2 schema)
    coexists with the new ``webhook_events`` table
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from harness.agents.jobs import JobStore
from harness.agents.webhook_store import WebhookEvent, WebhookEventStore


# === Init / schema ===

class TestWebhookStoreInit:
    async def test_init_creates_table(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        # Use a sibling path for the webhook DB. We re-use
        # ``auth_db_path`` here just for convenience; in production
        # it's ``<settings.db_path.parent>/agent-jobs.db``. The schema
        # creation is identical either way.
        await store.init()
        # Verify the table exists.
        async with aiosqlite.connect(store.db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='webhook_events'"
            ) as cur:
                row = await cur.fetchone()
        assert row is not None, "webhook_events table was not created"

    async def test_init_is_idempotent(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        # Second call must be a no-op (no exception, no duplicate
        # tables). The CREATE TABLE IF NOT EXISTS is the contract
        # that makes this safe.
        await store.init()

    async def test_init_coexists_with_merge_jobs(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """Both ``merge_jobs`` and ``webhook_events`` live in the same DB.

        Phase 2.3: the ``SCHEMA`` constant in ``jobs.py`` creates
        BOTH tables in one script (atomic migration). Creating a
        job in JobStore also creates the ``webhook_events`` table
        (idempotent). The webhook store can then use the same file
        without disturbing ``merge_jobs`` data.
        """
        db_path = isolated_settings["auth_db_path"]
        # First, create a job-store-shaped DB by creating a job.
        # ``JobStore`` doesn't expose ``init()`` — schema is created
        # lazily on first use via ``create()``, and that script
        # now creates BOTH tables atomically.
        job_store = JobStore(db_path)
        await job_store.create(
            worktree_id="wt-coexist", model="MiniMax-M2.7",
            prompt="seed", status="queued",
        )
        # The original job row is still there.
        recs = await job_store.list_recent(10)
        assert any(r.worktree_id == "wt-coexist" for r in recs)
        # The webhook store can use the same file.
        webhook_store = WebhookEventStore(db_path)
        await webhook_store.init()
        eid = await webhook_store.record_event(
            "coexist-1", "pull_request", "closed", {"n": 1},
        )
        assert eid is not None
        # And the original job row is STILL there (the webhook
        # init did not drop or disturb merge_jobs).
        recs2 = await job_store.list_recent(10)
        assert any(r.worktree_id == "wt-coexist" for r in recs2)


# === record_event ===

class TestRecordEvent:
    async def test_record_event_returns_id(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        event_id = await store.record_event(
            delivery_id="d-1",
            event_type="pull_request",
            action="closed",
            payload={"action": "closed", "number": 42},
        )
        assert event_id is not None
        assert isinstance(event_id, int)
        assert event_id >= 1

    async def test_record_event_duplicate_returns_none(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        # First insert: ok.
        first = await store.record_event(
            delivery_id="dup-1", event_type="pull_request",
            action="closed", payload={"a": 1},
        )
        assert first is not None
        # Second insert with same delivery_id: returns None.
        second = await store.record_event(
            delivery_id="dup-1", event_type="pull_request",
            action="closed", payload={"a": 2},  # different payload
        )
        assert second is None, "duplicate delivery_id should return None"
        # Verify only one row was inserted (the second payload is
        # silently dropped — the FIRST one is the source of truth).
        ev = await store.get_event("dup-1")
        assert ev is not None
        assert ev.payload == {"a": 1}

    async def test_record_event_different_delivery_ids(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        id1 = await store.record_event(
            "d-a", "pull_request", "closed", {"n": 1},
        )
        id2 = await store.record_event(
            "d-b", "pull_request", "closed", {"n": 2},
        )
        assert id1 is not None and id2 is not None
        assert id1 != id2


# === is_duplicate ===

class TestIsDuplicate:
    async def test_is_duplicate_false_for_new(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        assert await store.is_duplicate("new-id") is False

    async def test_is_duplicate_true_after_record(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        await store.record_event(
            "seen-1", "pull_request", "closed", {},
        )
        assert await store.is_duplicate("seen-1") is True


# === mark_processed ===

class TestMarkProcessed:
    async def test_mark_processed_flips_flag(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        eid = await store.record_event(
            "p-1", "pull_request", "closed", {},
        )
        ev = await store.get_event("p-1")
        assert ev is not None
        assert ev.processed is False
        await store.mark_processed(eid)
        ev2 = await store.get_event("p-1")
        assert ev2 is not None
        assert ev2.processed is True

    async def test_mark_processed_unknown_id_no_op(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        # No exception, no row created. Just a no-op UPDATE.
        await store.mark_processed(99999)


# === Counts ===

class TestCounts:
    async def test_count_unprocessed_starts_at_zero(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        assert await store.count_unprocessed() == 0
        assert await store.count_total() == 0

    async def test_count_unprocessed_after_records_and_marks(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        id1 = await store.record_event("c-1", "pull_request", "closed", {})
        await store.record_event("c-2", "check_run", "completed", {})
        id3 = await store.record_event("c-3", "pull_request_review", "submitted", {})
        # Mark 1 and 3 as processed.
        await store.mark_processed(id1)
        await store.mark_processed(id3)
        assert await store.count_total() == 3
        assert await store.count_unprocessed() == 1
