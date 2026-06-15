"""Phase 3 v1.2.0: scratchpad audit log.

Records scratchpad events to an optional JSONL mirror at
``data/audit/scratchpad-YYYY-MM-DD.ndjson``. Mirrors the Phase 3.5
:class:`~harness.context.compaction_audit.CompactionAudit` pattern
(same shape, same daily-rotation, same off-by-default behavior).

Off by default (``settings.scratchpad_audit_log=False``) so the
default install has zero audit overhead. The store emits a
``logger.info("scratchpad.<event> ...")` line regardless — audit
adds the JSONL file, not the structured log.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ScratchpadAudit:
    """Writes scratchpad events to an optional JSONL mirror.

    Args:
        audit_dir: Optional directory for the JSONL mirror. Created
                   on first write if it doesn't exist.
        enabled:   When False, ``record()`` is a no-op. Operators can
                   disable audit without unwiring the runtime.
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
        """Record a single scratchpad event.

        Args:
            event:      Event name (``"write"``, ``"read"``,
                        ``"promote"``, ``"plan_step"``, ``"mark_done"``,
                        ``"l0_cap_exceeded"``). Logged verbatim.
            session_id: The session this event belongs to.
            **fields:   Additional event-specific fields (e.g.
                        ``level``, ``note_id``, ``step_id``,
                        ``size_bytes``, ``tags_count``, ``deps_count``).
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
            logger.info(
                "scratchpad.audit %s", json.dumps(record, ensure_ascii=False),
            )
            return
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            path = self.audit_dir / f"scratchpad-{day}.ndjson"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            # Best-effort — log and move on. Audit must never break
            # the scratchpad tool call (which would cascade into the
            # chat loop).
            logger.warning("scratchpad audit mirror write failed: %s", e)
