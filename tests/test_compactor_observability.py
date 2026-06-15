"""Tests for Phase 3.5 observability: structured logs + JSONL audit log."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from harness.agents.compact_store import CompactRecord, CompactStore
from harness.config import Settings
from harness.context.compaction import ContextCompactor
from harness.context.compaction_audit import CompactionAudit


# === Test fixtures ===

class _FakeRouter:
    """Minimal async router for testing."""

    def __init__(self, summary: str = "summary text") -> None:
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


# === Structured logs ===

class TestStructuredLogs:
    async def test_cache_hit_emits_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A cache hit emits a ``compactor.cache_hit`` log line with
        session_id, version, saved_tokens, saved_ms."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="primary")
        compactor = ContextCompactor(
            settings=settings,
            router=router,
            session_id="log-test",
            store=store,
        )
        # Pre-populate the cache.
        from harness.context.compaction import ContextCompactor as CC
        history = _long_history()
        source_hash = CC._source_hash(history)
        await store.insert(CompactRecord(
            session_id="log-test",
            version=0,
            source_hash=source_hash,
            original_tokens=10_000,
            compacted_tokens=200,
            original_message_count=len(history),
            kept_message_ids=[],
            summary="cached",
            model="qwen3:8b",
            trigger_kind="auto_load_history",
            outcome="ok",
            created_at=0.0,
            duration_ms=50.0,
        ))
        with caplog.at_level(logging.INFO, logger="harness.context.compaction"):
            await compactor.maybe_compact(
                history, "qwen3:8b", session_id="log-test",
            )
        # Find the cache_hit log line.
        cache_hit_lines = [
            r for r in caplog.records if "compactor.cache_hit" in r.message
        ]
        assert len(cache_hit_lines) == 1
        msg = cache_hit_lines[0].message
        assert "log-test" in msg
        assert "version=" in msg

    async def test_run_emits_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A successful run emits a ``compactor.run`` log line with
        outcome, version, token counts, duration_ms."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="fresh")
        compactor = ContextCompactor(
            settings=settings,
            router=router,
            session_id="log-run",
            store=store,
        )
        with caplog.at_level(logging.INFO, logger="harness.context.compaction"):
            await compactor.maybe_compact(
                _long_history(), "qwen3:8b", session_id="log-run",
            )
        run_lines = [
            r for r in caplog.records if "compactor.run" in r.message
        ]
        assert len(run_lines) == 1
        msg = run_lines[0].message
        assert "outcome=ok" in msg
        assert "log-run" in msg

    async def test_persist_failed_emits_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing persist emits a WARNING log line."""

        class _FailStore(CompactStore):
            async def insert(self, record: CompactRecord) -> int:
                raise RuntimeError("disk full")

        store = _FailStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="x")
        compactor = ContextCompactor(
            settings=settings,
            router=router,
            session_id="log-fail",
            store=store,
        )
        with caplog.at_level(logging.WARNING, logger="harness.context.compaction"):
            await compactor.maybe_compact(
                _long_history(), "qwen3:8b", session_id="log-fail",
            )
        warn_lines = [
            r for r in caplog.records
            if "persist_compact failed" in r.message or
               "persist to compact_store failed" in r.message
        ]
        assert len(warn_lines) >= 1


# === JSONL audit log ===

class TestAuditLog:
    async def test_audit_disabled_writes_nothing(
        self, tmp_path: Path,
    ) -> None:
        """When ``enabled=False``, no JSONL file is created."""
        audit_dir = tmp_path / "audit"
        audit = CompactionAudit(audit_dir=audit_dir, enabled=False)
        audit.record("cache_hit", "sess-1", version=1, saved_tokens=500)
        # No files created.
        assert not audit_dir.exists() or list(audit_dir.glob("*.ndjson")) == []

    async def test_cache_hit_creates_jsonl_line(self, tmp_path: Path) -> None:
        """When enabled, a cache_hit event writes one JSONL line."""
        audit_dir = tmp_path / "audit"
        audit = CompactionAudit(audit_dir=audit_dir, enabled=True)
        audit.record("cache_hit", "sess-1", version=2, saved_tokens=500, duration_ms=12.3)
        # Find the file.
        files = list(audit_dir.glob("compaction-*.ndjson"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "cache_hit"
        assert record["session_id"] == "sess-1"
        assert record["version"] == 2
        assert record["saved_tokens"] == 500
        assert record["duration_ms"] == 12.3
        assert "ts" in record

    async def test_run_event_written(self, tmp_path: Path) -> None:
        """A run event has outcome, version, token counts."""
        audit_dir = tmp_path / "audit"
        audit = CompactionAudit(audit_dir=audit_dir, enabled=True)
        audit.record(
            "run", "sess-2", outcome="ok", version=1,
            original_tokens=10_000, compacted_tokens=300, duration_ms=200.0,
        )
        files = list(audit_dir.glob("compaction-*.ndjson"))
        assert len(files) == 1
        record = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert record["event"] == "run"
        assert record["outcome"] == "ok"
        assert record["original_tokens"] == 10_000

    async def test_persist_failed_event_written(self, tmp_path: Path) -> None:
        """A persist_failed event has the error message."""
        audit_dir = tmp_path / "audit"
        audit = CompactionAudit(audit_dir=audit_dir, enabled=True)
        audit.record("persist_failed", "sess-3", error="disk full")
        files = list(audit_dir.glob("compaction-*.ndjson"))
        record = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert record["event"] == "persist_failed"
        assert record["error"] == "disk full"

    async def test_audit_appended_across_events(self, tmp_path: Path) -> None:
        """Multiple events in the same day go to the same file (append-only)."""
        audit_dir = tmp_path / "audit"
        audit = CompactionAudit(audit_dir=audit_dir, enabled=True)
        for i in range(3):
            audit.record("cache_hit", f"sess-{i}", version=i)
        files = list(audit_dir.glob("compaction-*.ndjson"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    async def test_audit_via_compactor_writes_three_lines_for_full_flow(
        self, tmp_path: Path,
    ) -> None:
        """A compactor with audit enabled writes cache_hit OR run
        line(s) during a single compact call."""
        audit_dir = tmp_path / "audit"
        # Pre-create the directory so the test doesn't depend on the
        # compactor's mkdir path (which is also fine, but explicit
        # is clearer for debugging).
        audit_dir.mkdir(parents=True, exist_ok=True)
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        settings = _build_settings(tmp_path, persistent_store=True)
        router = _FakeRouter(summary="audit-summary")
        audit = CompactionAudit(audit_dir=audit_dir, enabled=True)
        compactor = ContextCompactor(
            settings=settings,
            router=router,
            session_id="audit-flow",
            store=store,
            audit=audit,
        )
        await compactor.maybe_compact(
            _long_history(), "qwen3:8b", session_id="audit-flow",
        )
        # Debug aid: print audit dir contents.
        files = list(audit_dir.glob("compaction-*.ndjson"))
        if not files:
            # Look at any files in the audit_dir (incl. hidden).
            print(f"AUDIT_DIR: {audit_dir}, exists={audit_dir.exists()}, "
                  f"contents={list(audit_dir.iterdir())}")
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        # Should have a "run" event (cache miss + slow path).
        events = [json.loads(line)["event"] for line in lines]
        assert "run" in events
        # All events belong to the same session.
        for line in lines:
            assert json.loads(line)["session_id"] == "audit-flow"


# === Audit log (fallback to logging only) ===

class TestAuditFallbackToLogging:
    async def test_audit_dir_none_logs_to_logger(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When ``audit_dir=None`` but ``enabled=True``, the audit
        falls back to the standard logger — useful for dev / test
        where no on-disk persistence is desired."""
        audit = CompactionAudit(audit_dir=None, enabled=True)
        with caplog.at_level(logging.INFO, logger="harness.context.compaction_audit"):
            audit.record("cache_hit", "sess-fallback", version=1)
        # At least one log line emitted with the event name.
        assert any("compaction.audit" in r.message for r in caplog.records)
