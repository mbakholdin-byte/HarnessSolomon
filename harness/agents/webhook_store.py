"""Webhook event store — Phase 2.3 (inbound GitHub webhooks) +
Phase 4.13B (outbound delivery hardening: auto-disable, DLQ,
secret rotation).

This module is the durability layer for inbound webhook events AND
the persistence layer for outbound delivery state (auto-disable
counters, DLQ entries, secret versioning). It sits next to
:mod:`harness.agents.jobs` (the merge-queue job store) in the same
SQLite file but in separate tables.

Design choices
--------------

- **Inbound (``webhook_events`` table):** GitHub may redeliver a
  webhook, and we need a way to detect "I've already seen this
  delivery_id" without re-running the handler. ``UNIQUE(delivery_id)``
  is the idempotency primitive.
- **Outbound config (``outbound_webhooks`` table, Phase 4.13B):** One
  row per outbound URL. Tracks ``consecutive_failures`` and
  ``disabled_at`` for the auto-disable circuit breaker. The
  dispatcher reads the row before each delivery to skip disabled
  targets, and writes back the counter on failure/success.
- **Outbound DLQ (``outbound_dlq`` table, Phase 4.13B):** A delivery
  that exhausts all retries is persisted here with the original
  payload + last error. Operators can list / replay via the
  ``/api/v1/observability/webhooks/dlq`` admin endpoint. A replayed
  entry is marked ``replayed_at`` (kept for audit, not re-replayed).
- **Secret rotation (``secret_version`` column, Phase 4.13B):** Each
  outbound row carries a ``secret_version`` (default 1). The
  dispatcher resolves the current signing secret via
  :func:`resolve_outbound_secret`; version 1 reads the legacy
  ``WEBHOOK_SECRET`` env var for backward compat.

Trust boundary
--------------

This module imports only :mod:`harness.agents.jobs` for the shared
``SCHEMA`` constant (so the ``webhook_events`` table is created
atomically with the merge-queue tables) and :mod:`aiosqlite` /
stdlib. It does NOT import from :mod:`harness.server` — Phase 2.0's
boundary is preserved (verified by
``tests/test_outbound.py::test_outbound_does_not_import_harness_server``
and the trust-boundary tests in Phase 4.13B).
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


# === Schema (Phase 4.13B — outbound delivery state) =====================
#
# These tables are created in :meth:`WebhookEventStore.init` AFTER the
# shared ``SCHEMA`` from :mod:`harness.agents.jobs`. They live in the
# same SQLite file but are owned by this module (so we can evolve them
# without touching the merge-queue schema).

_OUTBOUND_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS outbound_webhooks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    url                   TEXT NOT NULL UNIQUE,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    disabled_at           TEXT,
    secret_version        INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbound_webhooks_disabled
    ON outbound_webhooks(disabled_at);

CREATE TABLE IF NOT EXISTS outbound_dlq (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id    INTEGER NOT NULL,
    url           TEXT NOT NULL,
    event_kind    TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    last_error    TEXT NOT NULL DEFAULT '',
    failed_at     TEXT NOT NULL,
    replayed_at   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (webhook_id) REFERENCES outbound_webhooks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_outbound_dlq_replayed
    ON outbound_dlq(replayed_at, failed_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbound_dlq_webhook
    ON outbound_dlq(webhook_id);
"""


# === Dataclasses ========================================================


@dataclass(frozen=True)
class WebhookEvent:
    """One row in the ``webhook_events`` table.

    Immutable. The ``payload`` is stored as a JSON string; this
    dataclass exposes it as a ``dict`` for ergonomic use in the
    handler.
    """

    id: int
    delivery_id: str
    event_type: str
    action: str | None
    received_at: str
    processed: bool
    payload: dict[str, Any]


@dataclass(frozen=True)
class OutboundWebhook:
    """One row in the ``outbound_webhooks`` table (Phase 4.13B).

    Tracks the auto-disable circuit breaker state for a single
    outbound URL. The dispatcher reads this row before every
    delivery to decide whether to skip (``disabled_at is not None``),
    and writes back the failure counter + disabled_at on failure.

    Attributes:
        id: SQLite row id.
        url: The outbound webhook URL (UNIQUE).
        consecutive_failures: Number of consecutive failed deliveries
            (reset to 0 on the next success). When this reaches
            ``auto_disable_threshold`` the dispatcher sets
            ``disabled_at`` and stops sending.
        disabled_at: ISO timestamp when the URL was auto-disabled.
            ``None`` means the URL is active.
        secret_version: Which signing secret version to use.
            Default 1 → legacy ``WEBHOOK_SECRET`` env var.
        created_at / updated_at: Row-level bookkeeping timestamps.
    """

    id: int
    url: str
    consecutive_failures: int
    disabled_at: str | None
    secret_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DlqEntry:
    """One row in the ``outbound_dlq`` table (Phase 4.13B Drift 2).

    A delivery that exhausted all retries. Operators can replay it
    via the admin endpoint; on success the row is marked
    ``replayed_at`` and not re-replayed.

    Attributes:
        id: SQLite row id.
        webhook_id: FK to :class:`OutboundWebhook.id`.
        url: Denormalised URL (so listing works even if the config
            row is deleted via CASCADE — we keep the URL snapshot).
        event_kind: The ``kind`` field from the original event.
        payload: The original event payload (JSON-decoded dict).
        last_error: The final error message that caused the DLQ
            entry (truncated by the dispatcher to ~200 chars).
        failed_at: ISO timestamp of the DLQ insert.
        replayed_at: ISO timestamp of a successful replay, or
            ``None`` if the entry has not been replayed yet.
        attempts: Number of delivery attempts before the DLQ insert
            (typically ``max_retries + 1``).
    """

    id: int
    webhook_id: int
    url: str
    event_kind: str
    payload: dict[str, Any]
    last_error: str
    failed_at: str
    replayed_at: str | None
    attempts: int


# === Secret resolution ==================================================

#: Default secret version for new outbound rows and legacy lookups.
DEFAULT_SECRET_VERSION: int = 1

#: Default auto-disable threshold (consecutive failures before a URL
#: is parked). Operators can override per-call; this is the fallback.
DEFAULT_AUTO_DISABLE_THRESHOLD: int = 10


def resolve_outbound_secret(
    secret_version: int,
    *,
    secret_env_var: str = "WEBHOOK_SECRET",
) -> str | None:
    """Resolve the signing secret for an outbound webhook version.

    Phase 4.13B Drift 3 — secret rotation support. Version 1 reads
    the legacy ``WEBHOOK_SECRET`` env var (backward compat with
    Phase 2.5 bearer-token dispatcher). Future versions (2+) would
    read ``WEBHOOK_SECRET_V2``, etc. — the convention is one env
    var per version, so rotation is atomic (set the new env var,
    bump ``secret_version`` on the row, restart).

    Args:
        secret_version: The row's ``secret_version`` column value.
        secret_env_var: Override the base env var name (for tests).

    Returns:
        The secret string, or ``None`` if the env var is unset. A
        ``None`` return means "no signing secret configured" — the
        dispatcher falls back to the bearer token (Phase 2.5 compat).
    """
    import os

    if secret_version <= DEFAULT_SECRET_VERSION:
        return os.environ.get(secret_env_var) or None
    # Future versions: WEBHOOK_SECRET_V2, _V3, ...
    return os.environ.get(f"{secret_env_var}_V{secret_version}") or None


# === Store ===

class WebhookEventStore:
    """Async SQLite store for inbound webhook events + outbound state.

    Same file as :class:`harness.agents.jobs.JobStore` (the
    ``webhook_events`` table is created in the shared ``SCHEMA``),
    different tables. A single ``init()`` call creates both, plus
    the Phase 4.13B ``outbound_webhooks`` / ``outbound_dlq`` tables.

    Args:
        db_path: Path to the SQLite file. Parent directories are
            created if they don't exist. We do NOT share the path
            with the main harness DB (``harness.db``) — this store
            lives next to ``agent-jobs.db``.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        # ``_initialized`` is module-level state per-path; we use a
        # per-instance flag because the test suite swaps the path.
        self._initialized: bool = False

    async def init(self) -> None:
        """Create all tables (inbound + outbound) on first connect.

        Idempotent. Calls :mod:`harness.agents.jobs.SCHEMA` for the
        ``webhook_events`` table (atomic with the merge-queue tables),
        then runs :data:`_OUTBOUND_SCHEMA` for the Phase 4.13B tables.
        """
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        from harness.agents.jobs import SCHEMA  # shared, atomic init
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.executescript(_OUTBOUND_SCHEMA)
            await db.commit()
        self._initialized = True

    # --- idempotency (inbound) ---

    async def is_duplicate(self, delivery_id: str) -> bool:
        """Fast ``SELECT 1`` check before parsing the payload.

        Called by the handler before doing any expensive work
        (HMAC verify, JSON parse, dispatch). Returns True if a row
        with this ``delivery_id`` already exists — even if it
        hasn't been ``processed`` yet (in which case a redelivery
        is treated as "still in flight" and we skip).
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
        """Insert a new inbound webhook event row. Returns the row id.

        Returns ``None`` if a row with the same ``delivery_id``
        already exists (redelivery). We catch the
        ``IntegrityError`` from the UNIQUE constraint and translate
        it to ``None`` so the handler can skip the dispatch.
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
            logger.info(
                "webhook_events: duplicate delivery_id=%s (event=%s, action=%s)",
                delivery_id, event_type, action,
            )
            return None

    async def mark_processed(self, event_id: int) -> None:
        """Flip ``processed=1`` after the handler finishes dispatching."""
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE webhook_events SET processed = 1 WHERE id = ?",
                (int(event_id),),
            )
            await db.commit()

    async def get_event(self, delivery_id: str) -> WebhookEvent | None:
        """Fetch a single inbound webhook event by its ``delivery_id``."""
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

    # --- ops (inbound) ---

    async def count_unprocessed(self) -> int:
        """Count inbound rows where ``processed = 0``."""
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM webhook_events WHERE processed = 0"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_total(self) -> int:
        """Count all inbound rows. For ops dashboards / tests."""
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM webhook_events"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    # --- outbound config (Phase 4.13B Drift 1 + 3) ---

    async def get_or_create_outbound(
        self,
        url: str,
        *,
        secret_version: int = DEFAULT_SECRET_VERSION,
    ) -> OutboundWebhook:
        """Get the config row for ``url``, creating it if missing.

        New rows start active (``disabled_at=None``,
        ``consecutive_failures=0``). The ``secret_version`` is only
        applied on first creation — subsequent calls keep the
        existing version (rotation is a separate admin action).
        """
        await self.init()
        now = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # INSERT OR IGNORE to handle the race-free first touch,
            # then SELECT the row (whether new or existing).
            await db.execute(
                """
                INSERT OR IGNORE INTO outbound_webhooks
                    (url, consecutive_failures, disabled_at,
                     secret_version, created_at, updated_at)
                VALUES (?, 0, NULL, ?, ?, ?)
                """,
                (url, int(secret_version), now, now),
            )
            await db.commit()
            async with db.execute(
                "SELECT * FROM outbound_webhooks WHERE url = ?",
                (url,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None  # INSERT OR IGNORE guarantees a row
        return _row_to_outbound(row)

    async def get_outbound(self, url: str) -> OutboundWebhook | None:
        """Read-only config row fetch. Returns ``None`` if unknown URL."""
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbound_webhooks WHERE url = ?",
                (url,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_outbound(row) if row else None

    async def record_outbound_success(self, url: str) -> None:
        """Reset the failure counter on a successful delivery.

        Called by the dispatcher after a 2xx response. Resets
        ``consecutive_failures`` to 0; ``disabled_at`` is left
        unchanged (a re-enable is an explicit admin action —
        success on a disabled URL shouldn't silently revive it,
        because the dispatcher skips disabled URLs entirely).
        """
        await self.init()
        now = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE outbound_webhooks
                SET consecutive_failures = 0, updated_at = ?
                WHERE url = ?
                """,
                (now, url),
            )
            await db.commit()

    async def record_outbound_failure(
        self,
        url: str,
        *,
        auto_disable_threshold: int = DEFAULT_AUTO_DISABLE_THRESHOLD,
    ) -> bool:
        """Increment the failure counter, auto-disable if threshold met.

        Called by the dispatcher after all retries are exhausted
        (or a 4xx that won't fix on retry). Returns ``True`` if the
        row was auto-disabled by THIS call (i.e. the counter just
        crossed the threshold). Returns ``False`` otherwise (still
        under threshold, or already disabled).

        Args:
            url: The outbound URL.
            auto_disable_threshold: Consecutive failure count at
                which the URL is parked. Defaults to
                :data:`DEFAULT_AUTO_DISABLE_THRESHOLD` (10).
        """
        await self.init()
        # Ensure the row exists so the UPDATE hits something.
        await self.get_or_create_outbound(url)
        now = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                UPDATE outbound_webhooks
                SET consecutive_failures = consecutive_failures + 1,
                    updated_at = ?
                WHERE url = ?
                RETURNING consecutive_failures, disabled_at
                """,
                (now, url),
            )
            row = await cur.fetchone()
            await db.commit()
        if row is None:
            return False
        new_count = int(row["consecutive_failures"])
        already_disabled = row["disabled_at"] is not None
        if new_count >= auto_disable_threshold and not already_disabled:
            disabled_at = _utcnow().isoformat()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    UPDATE outbound_webhooks
                    SET disabled_at = ?, updated_at = ?
                    WHERE url = ? AND disabled_at IS NULL
                    """,
                    (disabled_at, disabled_at, url),
                )
                await db.commit()
            logger.warning(
                "outbound_webhooks: auto-disabled url=%s after %d "
                "consecutive failures (threshold=%d)",
                url, new_count, auto_disable_threshold,
            )
            return True
        return False

    async def enable_outbound(self, url: str) -> bool:
        """Re-enable a disabled outbound URL (admin action).

        Resets ``disabled_at`` to NULL AND ``consecutive_failures`` to 0
        (otherwise the next failure would immediately re-disable).
        Returns ``True`` if the row was disabled and is now active,
        ``False`` if it was already active (or unknown URL — no-op).
        """
        await self.init()
        now = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE outbound_webhooks
                SET disabled_at = NULL,
                    consecutive_failures = 0,
                    updated_at = ?
                WHERE url = ? AND disabled_at IS NOT NULL
                """,
                (now, url),
            )
            await db.commit()
            return cur.rowcount > 0

    async def rotate_outbound_secret(
        self,
        url: str,
        new_version: int,
    ) -> OutboundWebhook | None:
        """Bump the ``secret_version`` for ``url`` (Phase 4.13B Drift 3).

        The next delivery will resolve the new secret via
        :func:`resolve_outbound_secret`. Returns the updated row, or
        ``None`` if the URL is unknown.
        """
        if new_version < 1:
            raise ValueError(f"secret_version must be >= 1, got {new_version}")
        await self.init()
        now = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                UPDATE outbound_webhooks
                SET secret_version = ?, updated_at = ?
                WHERE url = ?
                RETURNING *
                """,
                (int(new_version), now, url),
            )
            row = await cur.fetchone()
            await db.commit()
        return _row_to_outbound(row) if row else None

    # --- DLQ (Phase 4.13B Drift 2) ---

    async def enqueue_dlq(
        self,
        *,
        url: str,
        event_kind: str,
        payload: dict[str, Any],
        last_error: str,
        attempts: int,
    ) -> int:
        """Persist a failed delivery to the DLQ.

        Called by the dispatcher after all retries are exhausted.
        The ``webhook_id`` is resolved from the config row (created
        on the fly if missing, so the DLQ entry is never orphaned).
        Returns the new DLQ row id.
        """
        await self.init()
        cfg = await self.get_or_create_outbound(url)
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        failed_at = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO outbound_dlq
                    (webhook_id, url, event_kind, payload,
                     last_error, failed_at, attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cfg.id, url, event_kind, body,
                 last_error[:500], failed_at, int(attempts)),
            )
            await db.commit()
            dlq_id = int(cur.lastrowid)
        logger.info(
            "outbound_dlq: enqueued id=%d url=%s kind=%s attempts=%d",
            dlq_id, url, event_kind, attempts,
        )
        return dlq_id

    async def list_dlq(
        self,
        *,
        limit: int = 100,
        include_replayed: bool = False,
    ) -> list[DlqEntry]:
        """List recent DLQ entries (Phase 4.13B Drift 2).

        Args:
            limit: Max entries (default 100, clamped to [1, 1000]).
            include_replayed: If False (default), only entries with
                ``replayed_at IS NULL`` are returned. If True, all
                entries are returned (for audit history).
        """
        await self.init()
        clamped = max(1, min(1000, int(limit)))
        where = "" if include_replayed else "WHERE replayed_at IS NULL"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT * FROM outbound_dlq {where}
                ORDER BY failed_at DESC
                LIMIT ?
                """,
                (clamped,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dlq(r) for r in rows]

    async def get_dlq_entry(self, dlq_id: int) -> DlqEntry | None:
        """Fetch a single DLQ entry by id. Returns ``None`` if missing."""
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM outbound_dlq WHERE id = ?",
                (int(dlq_id),),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_dlq(row) if row else None

    async def mark_dlq_replayed(self, dlq_id: int) -> bool:
        """Mark a DLQ entry as replayed (after a successful resend).

        Returns ``True`` if the row was updated, ``False`` if it was
        already replayed or doesn't exist.
        """
        await self.init()
        replayed_at = _utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE outbound_dlq
                SET replayed_at = ?
                WHERE id = ? AND replayed_at IS NULL
                """,
                (replayed_at, int(dlq_id)),
            )
            await db.commit()
            return cur.rowcount > 0


# === Row mappers (module-private) ======================================


def _row_to_outbound(row: aiosqlite.Row) -> OutboundWebhook:
    """Map a SELECT row to :class:`OutboundWebhook`."""
    return OutboundWebhook(
        id=int(row["id"]),
        url=str(row["url"]),
        consecutive_failures=int(row["consecutive_failures"]),
        disabled_at=row["disabled_at"],
        secret_version=int(row["secret_version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_dlq(row: aiosqlite.Row) -> DlqEntry:
    """Map a SELECT row to :class:`DlqEntry`."""
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except json.JSONDecodeError:
        payload = {"_raw": row["payload"]}
    return DlqEntry(
        id=int(row["id"]),
        webhook_id=int(row["webhook_id"]),
        url=str(row["url"]),
        event_kind=str(row["event_kind"]),
        payload=payload,
        last_error=str(row["last_error"]),
        failed_at=str(row["failed_at"]),
        replayed_at=row["replayed_at"],
        attempts=int(row["attempts"]),
    )


__all__ = [
    "DEFAULT_AUTO_DISABLE_THRESHOLD",
    "DEFAULT_SECRET_VERSION",
    "DlqEntry",
    "OutboundWebhook",
    "WebhookEvent",
    "WebhookEventStore",
    "resolve_outbound_secret",
]
