"""Phase 3.5: compaction audit log.

Records compaction events to an optional JSONL mirror at
``data/audit/compaction-YYYY-MM-DD.ndjson``. Mirrors the Phase 3
:redaction:`~harness.redaction.audit.RedactionAudit` pattern but is
simpler — compaction events go to a single sink (the audit log) and
do not need to join with the JobStore event log.

Off by default (``settings.compaction_audit_log=False``) so the
default install has zero audit overhead.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CompactionAudit:
    """Writes compaction events to an optional JSONL mirror.

    Args:
        audit_dir: Optional directory for the JSONL mirror. Created
                   on first write if it doesn't exist.
        enabled:   When False, ``record()`` is a no-op. Operators can
                   disable audit without unwiring the compactor.
    """

    def __init__(
        self,
        *,
        audit_dir: Path | None = None,
        enabled: bool = False,
    ) -> None:
        self.audit_dir = audit_dir
        self.enabled = enabled

    def record(
        self,
        event: str,
        session_id: str,
        **fields: Any,
    ) -> None:
        """Record a single compaction event.

        Args:
            event:      Event name (``"cache_hit"``, ``"run"``,
                        ``"persist_failed"``). Logged verbatim.
            session_id: The session this event belongs to.
            **fields:   Additional event-specific fields (e.g.
                        ``version``, ``original_tokens``,
                        ``compacted_tokens``, ``duration_ms``,
                        ``outcome``, ``error``).
        """
        if not self.enabled:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "session_id": session_id,
            **fields,
        }
        if self.audit_dir is None:
            # No audit_dir configured — fall back to logging only.
            logger.info("compaction.audit %s", json.dumps(record, ensure_ascii=False))
            return
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = self.audit_dir / f"compaction-{day}.ndjson"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            # Best-effort — log and move on. Audit must never break
            # the compactor (which would cascade into the chat loop).
            logger.warning("compaction audit mirror write failed: %s", e)
