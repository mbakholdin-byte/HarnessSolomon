"""Phase 3: redaction audit log.

Records redaction events to two optional sinks:

  1. JobStore event log (``JobStore.append_event(kind="redaction", payload=...)``)
     — queryable, joins with the rest of the job lifecycle.
  2. JSONL mirror (``data/audit/redaction-YYYY-MM-DD.ndjson``) — append-only,
     rotated daily, easy to ship to a log aggregator.

Both sinks are off by default (``settings.redaction_audit_log=False``) so
the default install has zero audit overhead.

The class is constructed once in ``server/app.py`` lifespan and DI'd into
the 9 sink-point wrappers. It is **not** thread-safe; for the AsyncIO
event loop the implicit single-thread assumption holds.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.redaction.engine import RedactionMatch, scan

if TYPE_CHECKING:
    from harness.agents.jobs import JobStore

logger = logging.getLogger(__name__)


class RedactionAudit:
    """Writes redaction events to JobStore + optional JSONL mirror.

    Args:
        store:        Optional ``JobStore`` for event-log persistence.
        audit_dir:    Optional directory for the JSONL mirror. Created
                      on first write if it doesn't exist.
        enabled:      When False, ``record()`` is a no-op. Operators can
                      disable audit without unwiring sinks.
    """

    def __init__(
        self,
        *,
        store: "JobStore | None" = None,
        audit_dir: Path | None = None,
        enabled: bool = False,
    ) -> None:
        self.store = store
        self.audit_dir = audit_dir
        self.enabled = enabled

    def record(
        self,
        sink: str,
        text: str,
        *,
        job_id: str | None = None,
        categories: set[str] | None = None,
    ) -> list[RedactionMatch]:
        """Scan ``text`` for matches and (if enabled) persist them.

        Returns the full match list regardless of ``enabled`` — callers
        can still inspect the result for their own purposes (e.g. log
        a one-line summary).

        Args:
            sink:       Where the redaction happened ("llm_messages",
                        "pr_title", "outbound_webhook", etc.). Logged
                        verbatim.
            text:       The text that was scanned.
            job_id:     Optional job id (for joining with JobStore events).
            categories: Optional pattern set (forwarded to ``scan()``).

        Returns:
            List of ``RedactionMatch`` (caller can inspect for counts).
        """
        matches = scan(text, categories=categories)
        if not self.enabled or not matches:
            return matches
        # Aggregate per-category counts — never log the original secret.
        counts: dict[str, int] = {}
        for m in matches:
            counts[m.category] = counts.get(m.category, 0) + 1
        ts = datetime.now(timezone.utc).isoformat()
        record = {
            "ts": ts,
            "sink": sink,
            "job_id": job_id,
            "counts": counts,
            "total": len(matches),
        }
        # Sink 1: JSONL mirror
        if self.audit_dir is not None:
            try:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                path = self.audit_dir / f"redaction-{day}.ndjson"
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("redaction audit mirror write failed: %s", e)
        # Sink 2: JobStore event log (best-effort — JobStore may be None
        # for non-job contexts like CLI startup).
        if self.store is not None and job_id is not None:
            try:
                # Append to the events table (no schema change required).
                # ``append_event`` is async, so we use a sync best-effort
                # write here: log a warning if it fails rather than raise.
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # Fire-and-forget: schedule on the running loop.
                        loop.create_task(
                            self.store.append_event(
                                job_id=job_id,
                                kind="redaction",
                                payload=record,
                            )
                        )
                    else:
                        loop.run_until_complete(
                            self.store.append_event(
                                job_id=job_id,
                                kind="redaction",
                                payload=record,
                            )
                        )
                except RuntimeError:
                    # No running loop — log a warning, do not raise.
                    logger.warning(
                        "redaction audit: no event loop to record "
                        "JobStore event (sink=%s, count=%d)",
                        sink,
                        len(matches),
                    )
            except Exception as e:  # noqa: BLE001 — audit is best-effort
                logger.warning(
                    "redaction audit: JobStore append failed: %s", e,
                )
        return matches
