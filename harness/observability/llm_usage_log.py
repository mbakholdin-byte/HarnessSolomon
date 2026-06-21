"""Phase 7.6: NDJSON logger for LLM usage events.

Writes one JSON line per LLM completion to a configurable file.
Used by calibration parser in Phase 7.5+.

Trust boundary: stdlib only. Does NOT import from ``harness.agents``,
``harness.server``, or ``harness.hooks``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.observability.llm_usage_log")


class LlmUsageLogger:
    """Append-only NDJSON logger for LLM usage events.

    Writes one JSON line per event to a configurable file path.
    Thread-safe through atomic append (OS-level guarantee for
    ``open("a")`` writes smaller than PIPE_BUF on POSIX; on Windows
    we rely on the fact that write calls happen from the asyncio
    thread — one writer per server process).

    Typical usage::

        logger = LlmUsageLogger(path=Path("data/llm_usage.jsonl"))
        logger.log_usage({
            "event": "llm_completion",
            "model": "MiniMax-M2.7",
            "tier": "T2",
            "prompt_tokens": 1234,
            "completion_tokens": 567,
            "total_tokens": 1801,
            "cost_usd": 0.001234,
            "duration_s": 1.234,
            "status": "ok",
        })
    """

    def __init__(self, path: Path | None = None, enabled: bool = True) -> None:
        self._path = path
        self._enabled = enabled

    def log_usage(self, event: dict[str, Any]) -> None:
        """Append a usage event to the NDJSON log.

        Args:
            event: Dict with fields: ``model``, ``tier``,
                ``prompt_tokens``, ``completion_tokens``,
                ``cost_usd``, ``duration_s``, ``status``,
                ``timestamp`` (auto-added if missing),
                ``session_id`` (optional).

        If ``enabled=False`` or ``path=None``, this is a no-op.
        Never raises — failures are logged at debug level.
        """
        if not self._enabled or self._path is None:
            return
        try:
            if "timestamp" not in event:
                event["timestamp"] = datetime.now(timezone.utc).isoformat()
            line = json.dumps(event, ensure_ascii=False, default=str)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.debug("llm_usage_log write failed", exc_info=True)

    @property
    def path(self) -> Path | None:
        """Return the configured file path (or None)."""
        return self._path

    @property
    def enabled(self) -> bool:
        """Return whether logging is enabled."""
        return self._enabled

    def flush(self) -> None:
        """No-op for file I/O (already flushed per line).

        Provided for protocol compatibility with buffered loggers.
        """
