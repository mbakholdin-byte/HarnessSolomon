"""Phase 4.1: JsonlLogger — thread-safe structured log writer.

Append-only NDJSON writer, mirror ``harness/hooks/audit.py:HookAuditSink``.
Daily rotation by date suffix. Thread-safe via single ``threading.Lock``.

Trust boundary: stdlib only. No ``harness.agents`` / ``harness.server``
imports (Plan B2 mirror).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.observability.events import LogEvent

logger = logging.getLogger(__name__)


class JsonlLogger:
    """Append-only NDJSON writer for structured log events.

    Thread-safe: a single ``threading.Lock`` guards the file handle.
    Each ``emit`` opens, writes, and closes the line — slower but
    more robust against crashes (no half-line state in kernel buffer).

    Rotation: daily (file suffix ``-YYYY-MM-DD.jsonl``). Size-based
    rotation is the caller's responsibility (open new file when
    current exceeds ``observability_log_max_file_size_mb``).

    Example::

        sink = JsonlLogger(Path("data/logs"))
        sink.emit(LogEvent(event="llm_call", payload={"model": "gpt-4o"}))
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = Path(log_dir)
        self._lock = threading.Lock()
        self._current_date: str = ""

    def _path_for(self, when: datetime | None = None) -> Path:
        when = when or datetime.now(timezone.utc)
        return self._log_dir / f"harness-{when.strftime('%Y-%m-%d')}.jsonl"

    def emit(self, event: LogEvent) -> None:
        """Append one event as a JSONL line.

        Best-effort: failure to write is logged via stdlib logger
        but never raised (observability never breaks the application).
        """
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                default=str,  # fallback for non-serialisable values
            )
            path = self._path_for()
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "JsonlLogger: failed to emit %s: %s: %s",
                event.event, type(e).__name__, e,
            )

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        """Return the last ``n`` lines from today's log file."""
        path = self._path_for()
        if not path.exists():
            return []
        try:
            with self._lock:
                text = path.read_text(encoding="utf-8")
            lines = text.splitlines()[-n:]
            return [json.loads(line) for line in lines if line.strip()]
        except Exception:  # noqa: BLE001
            return []

    def cleanup(self, max_files: int) -> int:
        """Delete oldest files, keeping at most ``max_files``. Returns count deleted."""
        if max_files < 1:
            return 0
        try:
            with self._lock:
                files = sorted(
                    self._log_dir.glob("harness-*.jsonl"),
                    key=lambda p: p.name,
                    reverse=True,  # newest first
                )
                deleted = 0
                for f in files[max_files:]:
                    try:
                        f.unlink()
                        deleted += 1
                    except OSError:
                        pass
                return deleted
        except Exception:  # noqa: BLE001
            return 0


__all__ = ["JsonlLogger"]
