"""Phase 4.1: Tests for observability events + JsonlLogger."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.observability import JsonlLogger, LogEvent


class TestLogEvent:
    """LogEvent dataclass: frozen, to_dict, defaults."""

    def test_minimal_event(self) -> None:
        ev = LogEvent(event="test")
        assert ev.event == "test"
        assert ev.level == "INFO"
        assert ev.payload == {}
        assert ev.status == "ok"
        assert ev.session_id == ""
        assert ev.error is None

    def test_to_dict_serialisable(self) -> None:
        ev = LogEvent(
            event="llm_call",
            payload={"model": "gpt-4o", "tokens": 100},
            session_id="s1",
            latency_ms=250.5,
        )
        d = ev.to_dict()
        # JSON-serialisable.
        json.dumps(d)
        assert d["event"] == "llm_call"
        assert d["payload"]["model"] == "gpt-4o"
        assert d["latency_ms"] == 250.5

    def test_frozen(self) -> None:
        ev = LogEvent(event="x")
        with pytest.raises(Exception):  # FrozenInstanceError
            ev.event = "y"  # type: ignore[misc]


class TestJsonlLogger:
    """JsonlLogger: thread-safe NDJSON writer with daily rotation."""

    def test_creates_dir(self, tmp_path: Path) -> None:
        logger = JsonlLogger(tmp_path / "nested" / "deeper")
        logger.emit(LogEvent(event="test"))
        assert (tmp_path / "nested" / "deeper").is_dir()
        assert list((tmp_path / "nested" / "deeper").glob("harness-*.jsonl"))

    def test_emits_one_line(self, tmp_path: Path) -> None:
        logger = JsonlLogger(tmp_path)
        logger.emit(LogEvent(event="llm_call", payload={"model": "gpt-4o"}))
        files = list(tmp_path.glob("harness-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event"] == "llm_call"
        assert data["payload"]["model"] == "gpt-4o"

    def test_appends_multiple(self, tmp_path: Path) -> None:
        logger = JsonlLogger(tmp_path)
        for i in range(5):
            logger.emit(LogEvent(event="e", payload={"i": i}))
        files = list(tmp_path.glob("harness-*.jsonl"))
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5

    def test_unicode_safe(self, tmp_path: Path) -> None:
        logger = JsonlLogger(tmp_path)
        logger.emit(LogEvent(event="тест", payload={"язык": "русский"}))
        files = list(tmp_path.glob("harness-*.jsonl"))
        data = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert data["event"] == "тест"
        assert data["payload"]["язык"] == "русский"

    def test_tail_returns_last_n(self, tmp_path: Path) -> None:
        logger = JsonlLogger(tmp_path)
        for i in range(20):
            logger.emit(LogEvent(event="e", payload={"i": i}))
        tail = logger.tail(n=5)
        assert len(tail) == 5
        assert tail[-1]["payload"]["i"] == 19
        assert tail[0]["payload"]["i"] == 15

    def test_tail_empty_when_no_file(self, tmp_path: Path) -> None:
        logger = JsonlLogger(tmp_path)
        assert logger.tail(n=10) == []

    def test_handles_write_failure(self, tmp_path: Path) -> None:
        """Failure to write must NOT raise (B3 fail-open)."""
        # Use a path that can't be created (path is a file, not dir).
        not_a_dir = tmp_path / "not_a_dir"
        not_a_dir.write_text("blocking", encoding="utf-8")
        logger = JsonlLogger(not_a_dir)
        # Should not raise.
        logger.emit(LogEvent(event="x"))

    def test_cleanup_keeps_max_files(self, tmp_path: Path) -> None:
        logger = JsonlLogger(tmp_path)
        # Create 10 files with different dates.
        for i in range(10):
            (tmp_path / f"harness-2026-01-{i:02d}.jsonl").write_text(
                f'{{"event": "{i}"}}', encoding="utf-8"
            )
        deleted = logger.cleanup(max_files=3)
        assert deleted == 7
        remaining = sorted(tmp_path.glob("harness-*.jsonl"))
        assert len(remaining) == 3
        # Newest 3 should remain (Jan 09, 08, 07).
        assert "harness-2026-01-09.jsonl" in [p.name for p in remaining]
        assert "harness-2026-01-08.jsonl" in [p.name for p in remaining]
        assert "harness-2026-01-07.jsonl" in [p.name for p in remaining]

    def test_thread_safe(self, tmp_path: Path) -> None:
        """Multiple threads writing simultaneously — no line corruption."""
        import threading

        logger = JsonlLogger(tmp_path)

        def writer(thread_id: int) -> None:
            for i in range(50):
                logger.emit(LogEvent(
                    event="thread_test",
                    payload={"thread": thread_id, "i": i},
                ))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        files = list(tmp_path.glob("harness-*.jsonl"))
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        # 5 threads × 50 = 250 lines.
        assert len(lines) == 250
        # Every line is valid JSON.
        for line in lines:
            data = json.loads(line)
            assert data["event"] == "thread_test"
