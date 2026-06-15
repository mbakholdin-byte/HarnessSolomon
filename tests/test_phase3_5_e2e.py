"""End-to-end tests for Phase 3.5 full lifecycle (Step 4)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from harness.agents.compact_store import CompactStore
from harness.config import Settings
from harness.context.compaction import ContextCompactor
from harness.context.compaction_audit import CompactionAudit


# === Test fixtures ===

class _FakeRouter:
    """Async router that returns a fixed summary."""

    def __init__(self, summary: str = "e2e-summary") -> None:
        self.summary = summary
        self.call_count = 0

    async def completion(
        self,
        messages: list[dict],
        model: str,
        **kwargs: Any,
    ) -> Any:
        self.call_count += 1

        class _Resp:
            def __init__(self, content: str) -> None:
                self.content = content

        return _Resp(self.summary)


def _build_settings(
    tmp_path: Path,
    *,
    persistent_store: bool = True,
    audit_log: bool = False,
) -> Settings:
    return Settings(
        compaction_enabled=True,
        compaction_threshold_ratio=0.5,
        compaction_target_ratio=0.25,
        compaction_keep_recent_turns=50,  # forces summary path
        compaction_summarizer_model="qwen3:8b",
        compaction_summarizer_fallback="glm-4.7",
        compaction_persist_to_memory=False,
        compaction_persistent_store=persistent_store,
        compaction_cache_max_versions=5,
        compaction_audit_log=audit_log,
        db_path=tmp_path / "harness.db",
    )


def _long_history() -> list[dict[str, Any]]:
    """50 turns × 700 chars each → ~18K tokens (over 50% of 32K ctx)."""
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]
    for i in range(50):
        msgs.append({"role": "user", "content": f"User turn {i}: " + "x" * 680})
        msgs.append({
            "role": "assistant",
            "content": f"Assistant turn {i}: " + "y" * 680,
        })
    return msgs


# === E2E scenarios ===

class TestE2ELifecycle:
    async def test_full_lifecycle_miss_then_hit(
        self, tmp_path: Path,
    ) -> None:
        """Full session lifecycle:

        1. First compact call: cache miss → LLM call → persist to store
        2. Second compact call with SAME history: cache hit → no LLM
        3. SQLite table has exactly 1 row
        4. No version conflicts
        """
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="miss-summary")
        compactor = ContextCompactor(
            settings=settings,
            router=router,
            session_id="e2e-1",
            store=store,
        )
        history = _long_history()

        # Call 1: cache miss.
        await compactor.maybe_compact(
            history, "qwen3:8b", session_id="e2e-1",
        )
        assert router.call_count == 1
        assert await store.count() == 1

        # Call 2: same history → cache hit.
        await compactor.maybe_compact(
            history, "qwen3:8b", session_id="e2e-1",
        )
        # No new LLM call!
        assert router.call_count == 1
        # Still only 1 row in the store (same source_hash → no new version).
        assert await store.count() == 1

    async def test_new_message_invalidates_cache(
        self, tmp_path: Path,
    ) -> None:
        """Adding a new message changes source_hash → cache miss → new LLM call."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="v1-summary")
        compactor = ContextCompactor(
            settings=settings,
            router=router,
            session_id="e2e-2",
            store=store,
        )
        history_v1 = _long_history()

        # Compact v1: miss → call router.
        await compactor.maybe_compact(
            history_v1, "qwen3:8b", session_id="e2e-2",
        )
        assert router.call_count == 1
        assert await store.count() == 1

        # Add a new user message → source_hash changes.
        history_v2 = list(history_v1) + [
            {"role": "user", "content": "What was the previous question?"},
        ]
        # New router with different summary.
        router2 = _FakeRouter(summary="v2-summary")
        compactor2 = ContextCompactor(
            settings=settings,
            router=router2,
            session_id="e2e-2",
            store=store,
        )
        await compactor2.maybe_compact(
            history_v2, "qwen3:8b", session_id="e2e-2",
        )
        # New LLM call for v2.
        assert router2.call_count == 1
        # Two rows in the store now (v1 + v2).
        assert await store.count() == 2
        recs = await store.list_for_session("e2e-2")
        assert recs[0].summary == "v2-summary"
        assert recs[1].summary == "v1-summary"

    async def test_multi_session_isolation(self, tmp_path: Path) -> None:
        """Different sessions get separate cache entries."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="shared")
        compactor_a = ContextCompactor(
            settings=settings, router=router,
            session_id="session-A", store=store,
        )
        compactor_b = ContextCompactor(
            settings=settings, router=router,
            session_id="session-B", store=store,
        )
        history = _long_history()

        await compactor_a.maybe_compact(
            history, "qwen3:8b", session_id="session-A",
        )
        await compactor_b.maybe_compact(
            history, "qwen3:8b", session_id="session-B",
        )
        # Each session has its own row.
        assert len(await store.list_for_session("session-A")) == 1
        assert len(await store.list_for_session("session-B")) == 1
        assert await store.count() == 2


class TestE2EFailOpen:
    async def test_summariser_failure_returns_original(
        self, tmp_path: Path,
    ) -> None:
        """When T1 + T2 both fail, the compactor returns the trimmed
        history (no summary injected). No crash, no CompactStore row."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)

        class _FailingRouter:
            async def completion(
                self, messages: list[dict], model: str, **kwargs: Any,
            ) -> Any:
                raise RuntimeError("simulated LLM failure")

        compactor = ContextCompactor(
            settings=settings,
            router=_FailingRouter(),
            session_id="e2e-fail",
            store=store,
        )
        result = await compactor.maybe_compact(
            _long_history(), "qwen3:8b", session_id="e2e-fail",
        )
        # Result is a list (not None) — fail-open returns the trim.
        assert isinstance(result, list)
        assert len(result) > 0
        # No compact persisted (summariser returned empty).
        assert await store.count() == 0


class TestE2EAuditIntegration:
    async def test_audit_records_cache_miss_then_hit(
        self, tmp_path: Path,
    ) -> None:
        """With audit enabled, the JSONL file accumulates events
        across the full lifecycle."""
        audit_dir = tmp_path / "audit"
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="audit-summary")
        audit = CompactionAudit(audit_dir=audit_dir, enabled=True)
        compactor = ContextCompactor(
            settings=settings,
            router=router,
            session_id="e2e-audit",
            store=store,
            audit=audit,
        )
        history = _long_history()

        # Call 1: miss → emits "run" event.
        await compactor.maybe_compact(
            history, "qwen3:8b", session_id="e2e-audit",
        )
        # Call 2: hit → emits "cache_hit" event.
        await compactor.maybe_compact(
            history, "qwen3:8b", session_id="e2e-audit",
        )
        files = list(audit_dir.glob("compaction-*.ndjson"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        events = [json.loads(line)["event"] for line in lines]
        # run event first (miss), then cache_hit (second call).
        assert "run" in events
        assert "cache_hit" in events
        # All events tagged with the same session.
        for line in lines:
            assert json.loads(line)["session_id"] == "e2e-audit"


class TestE2ESchemaIntegrity:
    async def test_compact_store_schema_uses_agent_jobs_db(
        self, tmp_path: Path,
    ) -> None:
        """The ``compact_store`` table lives in the same DB file as
        ``merge_jobs`` (sibling, not separate file). This is critical
        for transactional consistency + WAL coordination."""
        db_path = tmp_path / "agent-jobs.db"
        store = CompactStore(db_path)
        await store.init()
        # Open the DB with stock sqlite3 and verify the table exists
        # alongside whatever else is there (the JobStore might add
        # its own tables in a real deployment, but for this test we
        # only care that compact_store was created).
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='compact_store'",
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "compact_store"
