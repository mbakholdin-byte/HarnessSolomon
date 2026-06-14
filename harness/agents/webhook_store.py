"""Webhook event store — Phase 2.3 (inbound GitHub webhooks).

This module is the durability layer for inbound webhook events. It
sits next to :mod:`harness.agents.jobs` (the merge-queue job store)
in the same SQLite file but in a separate ``webhook_events`` table.
The store's only job is **idempotency**: GitHub may redeliver a
webhook (e.g. the server was down, or the response timed out), and
we need a way to detect "I've already seen this delivery_id" without
re-running the handler.

Design choices
--------------

- **Delivery-id is the natural key.** GitHub stamps every event with
  ``X-GitHub-Delivery`` (a UUIDv4). We store it with a
  ``UNIQUE(delivery_id)`` constraint and use that as the idempotency
  primitive. The handler's ``record_event()`` returns ``None`` on a
  duplicate insert (the ``IntegrityError`` is caught and translated),
  so the dispatcher knows to skip re-processing.
- **Separate from JobStore.** Keeping the webhook log in its own
  table (in the same DB file) lets us run ``PRAGMA table_info`` /
  ``COUNT(*)`` queries without touching the merge-queue tables, and
  it makes future cleanup / archival policies (e.g. "drop
  webhook_events older than 30 days") trivial to implement.
- **No "processed" gate, only "seen".** A row in ``webhook_events``
  means "this delivery_id was received and verified". The handler
  itself does the rest (mark the job merged, etc.). We track
  ``processed`` as a soft flag for ops/observability — it tells you
  "we saw this delivery and our handler ran" vs. "we saw it but
  the handler crashed mid-way".

Trust boundary
--------------

This module imports only :mod:`harness.agents.jobs` for the
``webhook_events`` schema (which lives in the same ``SCHEMA``
string for atomic creation). It does NOT import from
:mod:`harness.server` — Phase 2.0's boundary is preserved.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


# === Helpers (mirrored from jobs.py) ===

def _utcnow() -> datetime:
    """UTC now without tzinfo (matches the main sqlite.py convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


# === Schemas ===

@dataclass(frozen=True)
class WebhookEvent:
    """One row in the ``webhook_events`` table.

    Immutable. The ``payload`` is stored as a JSON string; this
    dataclass exposes it as a ``dict`` for ergonomic use in the
    handler (``WebhookHandler.parse_github_payload`` reads from it
    when redelivering, or a developer reads it for debugging).
    """

    id: int
    delivery_id: str
    event_type: str
    action: str | None
    received_at: str
    processed: bool
    payload: dict[str, Any]


# === Store ===

class WebhookEventStore:
    """Async SQLite store for inbound webhook events.

    Same file as :class:`harness.agents.jobs.JobStore` (the
    ``webhook_events`` table is created in the same ``SCHEMA``),
    different table. A single ``init()`` call creates both.

    Args:
        db_path: Path to the SQLite file. Parent directories are
            created if they don't exist. We do NOT share the path
            with the main harness DB (``harness.db``) — this store
            lives next to ``agent-jobs.db`` (one level above
            ``harness.db``).
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        # ``_initialized`` is module-level state per-path; we use a
        # per-instance flag because the test suite swaps the path.
        self._initialized: bool = False

    async def init(self) -> None:
        """Create the ``webhook_events`` table on first connect.

        Idempotent: the ``CREATE TABLE IF NOT EXISTS`` is a no-op
        on a populated DB. The store shares the ``SCHEMA`` constant
        with :class:`JobStore`, so the table is created atomically
        with the merge-queue tables in the same ``executescript``
        call. (See :meth:`JobStore._ensure_schema`.)
        """
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        from harness.agents.jobs import SCHEMA  # shared, atomic init
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        self._initialized = True

    # --- idempotency ---

    async def is_duplicate(self, delivery_id: str) -> bool:
        """Fast ``SELECT 1`` check before parsing the payload.

        Called by the handler before doing any expensive work
        (HMAC verify, JSON parse, dispatch). Returns True if a row
        with this ``delivery_id`` already exists — even if it
        hasn't been ``processed`` yet (in which case a redelivery
        is treated as "still in flight" and we skip).

        This is a redundant fast-path: ``record_event`` ALSO
        catches the UNIQUE-violation, so skipping the call here
        only avoids the cost of HMAC + parse + dispatch. It does
        NOT change the correctness invariant.
        """
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM webhook_events WHERE delivery_id = ? LIMIT 1",
                (delivery_id,),
            ) as cur:
                row = await cur.fetchone()
        return row is not None

    async def record_event(
        self,
        delivery_id: str,
        event_type: str,
        action: str | None,
        payload: dict[str, Any],
    ) -> int | None:
        """Insert a new webhook event row. Returns the row id.

        Returns ``None`` if a row with the same ``delivery_id``
        already exists (redelivery). We catch the
        ``IntegrityError`` from the UNIQUE constraint and translate
        it to ``None`` so the handler can skip the dispatch.

        Args:
            delivery_id: GitHub's ``X-GitHub-Delivery`` header value.
            event_type:   GitHub's ``X-GitHub-Event`` header value.
            action:       The ``action`` field from the payload
                          (e.g. ``"closed"``, ``"submitted"``), or
                          ``None`` if the payload has no action.
            payload:      The full JSON payload as a dict. Stored
                          as JSON text. We do not enforce a schema
                          here — parsing/normalisation is the
                          handler's job.

        Note: ``processed`` defaults to 0. The handler should call
        :meth:`mark_processed` after it finishes dispatching so ops
        can see the rate of unprocessed events via
        :meth:`count_unprocessed`.
        """
        await self.init()
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        now = _utcnow().isoformat()
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cur = await db.execute(
                    """
                    INSERT INTO webhook_events
                        (delivery_id, event_type, action, received_at, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (delivery_id, event_type, action, now, body),
                )
                await db.commit()
                return cur.lastrowid
        except aiosqlite.IntegrityError:
            # UNIQUE(delivery_id) violation: this delivery was
            # already recorded. The handler treats this as
            # "duplicate, skip dispatch".
            logger.info(
                "webhook_events: duplicate delivery_id=%s (event=%s, action=%s)",
                delivery_id, event_type, action,
            )
            return None

    async def mark_processed(self, event_id: int) -> None:
        """Flip ``processed=1`` after the handler finishes dispatching.

        This is a soft flag for ops: it tells you "we saw this
        delivery and our handler ran without crashing" vs. "we saw
        it but the handler crashed mid-way". It does NOT gate
        dispatch — the handler can run again on redelivery (the
        idempotency check is on ``delivery_id`` in
        :meth:`is_duplicate`, not on ``processed``).
        """
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE webhook_events SET processed = 1 WHERE id = ?",
                (int(event_id),),
            )
            await db.commit()

    async def get_event(self, delivery_id: str) -> WebhookEvent | None:
        """Fetch a single webhook event by its ``delivery_id``.

        Used by tests + ops debug. Returns ``None`` if the event was
        never recorded.
        """
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, delivery_id, event_type, action, received_at,
                       processed, payload
                FROM webhook_events WHERE delivery_id = ?
                """,
                (delivery_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except json.JSONDecodeError:
            payload = {"_raw": row["payload"]}
        return WebhookEvent(
            id=row["id"], delivery_id=row["delivery_id"],
            event_type=row["event_type"], action=row["action"],
            received_at=row["received_at"],
            processed=bool(row["processed"]),
            payload=payload,
        )

    # --- ops ---

    async def count_unprocessed(self) -> int:
        """Count rows where ``processed = 0``.

        A non-zero count after a webhook storm means the handler
        crashed mid-dispatch on some deliveries. The handler is
        idempotent (re-runs are safe), so an operator can simply
        POST the same delivery to recover.
        """
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM webhook_events WHERE processed = 0"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_total(self) -> int:
        """Count all rows. For ops dashboards / tests."""
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM webhook_events"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "WebhookEvent",
    "WebhookEventStore",
]
