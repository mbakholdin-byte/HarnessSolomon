"""Phase 4.0: HookAuditSink — append-only NDJSON audit log.

When ``settings.hooks_audit_log=True``, every ``HookAggregate`` is
serialised to a JSONL file under
``<project_root>/data/audit/hooks-YYYY-MM-DD.ndjson`` (rotated daily).

Trust boundary: stdlib only. NO ``harness.agents`` or
``harness.server`` imports (Plan B2).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.hooks.context import HookAggregate


logger = logging.getLogger(__name__)


class HookAuditSink:
    """Append-only NDJSON writer for hook decisions.

    Thread-safe: a single ``threading.Lock`` guards the file handle.
    Each call to ``record`` opens, writes, and closes the line —
    this is slower but more robust against crashes (no half-line
    state in the kernel buffer).

    Example::

        sink = HookAuditSink(Path("data/audit"))
        sink.record(aggregate=agg, event="PreToolUse",
                    session_id="s1", agent_id="a1")
    """

    def __init__(self, audit_dir: Path) -> None:
        self._audit_dir = audit_dir
        self._lock = threading.Lock()

    def _path_for(self, when: datetime | None = None) -> Path:
        when = when or datetime.now(timezone.utc)
        return self._audit_dir / f"hooks-{when.strftime('%Y-%m-%d')}.ndjson"

    def record(
        self,
        *,
        aggregate: HookAggregate,
        event: str,
        session_id: str,
        agent_id: str,
        request_id: str = "",
    ) -> None:
        """Append one audit line for ``aggregate``.

        Best-effort: a failure to write is logged but never raised
        (audit is observation, not control).
        """
        try:
            self._audit_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": event,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "request_id": request_id,
                    "aggregate": aggregate.to_dict(),
                },
                ensure_ascii=False,
            )
            path = self._path_for()
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "HookAuditSink: failed to record %s: %s: %s",
                event,
                type(e).__name__,
                e,
            )

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        """Return the last ``n`` lines from today's audit file (best-effort)."""
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


__all__ = ["HookAuditSink"]
