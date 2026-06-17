"""Phase 4.3+ v1.12.0: ElicitationBroker — WebSocket-backed interactive prompts.

When the harness needs to ask the user a question, it can:
    1. Emit a ``Elicitation`` event (existing framework).
    2. The built-in ``confirm_dangerous`` hook falls back to a default
       answer (``abort``) when no human is in the loop (Phase 4.3 v1.10.0).
    3. If a WebSocket client is connected to ``/api/v1/elicitation/ws``,
       the broker publishes the question and waits for the user's
       answer — replacing the default with the real response.

This module is the broker (in-memory pending_questions dict, future-based
pub/sub). The WebSocket endpoint is in ``harness/server/routes/elicitation.py``.

Phase 4.8 v1.18.0 adds ``ElicitationDecisionStore`` — a SQLite-backed
persistent history of every publish/answer/timeout decision. The broker
records ``pending`` on publish and updates the row to ``answered`` /
``timed_out`` on ``wait()`` return. The store lives in the shared
``data/audit/agent-jobs.db`` (sibling of ``merge_jobs`` /
``scratchpad``) so operators can query it with the standard CLI/DB tooling.

Trust boundary: this module is stdlib + asyncio + dataclasses + sqlite3
only. No ``harness.agents`` / ``harness.server`` / ``harness.hooks``
imports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger("harness.elicitation")


@dataclass
class PendingQuestion:
    """A single pending Elicitation question awaiting a human answer.

    Attributes:
        question_id: UUID hex (12 chars).
        question: The question text (from Elicitation payload).
        options: Optional list of allowed answers.
        default_answer: Fallback answer if no human responds in time.
        future: ``asyncio.Future[str]`` — resolved when the human answers.
        created_at: ``time.monotonic()`` when published.
        timeout_s: How long to wait before falling back to default.
        session_id: Session scope (informational; "" when unknown).
            Phase 4.8 v1.18.0 — used by the decision store.
        request_id: Optional correlation id from the Elicitation
            payload. Phase 4.8 v1.18.0.
    """

    question_id: str
    question: str
    options: list[str]
    default_answer: str
    future: asyncio.Future[str] | None = None
    created_at: float = 0.0
    timeout_s: float = 30.0
    session_id: str = ""
    request_id: str | None = None

    def resolve_future(self) -> asyncio.Future[str]:
        """Lazily create the future on the current event loop.

        We can't use ``field(default_factory=...)`` because dataclass
        field factories run at instance creation time, which may be
        outside an event loop (e.g. in a sync test that calls
        ``broker.publish()``). Deferring to first ``wait()`` keeps the
        broker loop-agnostic.
        """
        if self.future is None:
            self.future = asyncio.get_running_loop().create_future()
        return self.future


# === Phase 4.8 v1.18.0: persistent decision history ===


@dataclass
class ElicitationDecisionRecord:
    """One row in the ``elicitation_decisions`` table.

    Attributes:
        decision_id: Stable primary key. We reuse ``question_id`` so
            ``record_decision`` on publish + the later ``UPDATE`` on
            answer/timeout hit the same row via ``INSERT OR REPLACE``.
            (Phase 4.8 deliberately uses REPLACE, not a two-step INSERT
            + UPDATE, to avoid a race where publish and wait happen on
            different threads — the SQLite lock is held per-statement.)
        session_id: Session scope (informational; may be "" when
            unknown — the broker is process-global).
        request_id: Optional correlation id from the Elicitation
            payload (for cross-referencing with hooks audit logs).
        question_id: The 12-char UUID hex returned by ``publish()``.
        question_preview: First ~120 chars of the question text (the
            full text is NOT stored — privacy hygiene).
        options: Allowed answers (JSON-encoded in SQLite as
            ``options_json``).
        default_answer: Fallback answer configured at publish time.
        decision: Lifecycle state — one of ``pending``, ``answered``,
            ``timed_out``.
        answer: The resolved answer (user-supplied or default). NULL
            while the row is in the ``pending`` state.
        source: Who resolved the decision. One of ``ws``, ``poll``,
            ``timeout``. NULL while ``pending``.
        latency_ms: Wall-clock milliseconds from publish to resolve.
            0 while ``pending``.
        ts: ``time.time()`` (wall clock, seconds since epoch) at the
            last mutation. Indexed with session_id for history queries.
    """

    decision_id: str
    session_id: str
    request_id: str | None
    question_id: str
    question_preview: str
    options: list[str]
    default_answer: str
    decision: str
    answer: str | None
    source: str | None
    latency_ms: int
    ts: float


class ElicitationDecisionStore:
    """SQLite-backed history of publish/answer/timeout decisions.

    The store shares the ``data/audit/agent-jobs.db`` file with the
    merge-queue / scratchpad stores (one extra table, no cross-table
    constraints). Connections use ``check_same_thread=False`` plus an
    internal ``threading.Lock`` — the broker calls ``record_decision``
    from the asyncio loop thread and the HTTP/CLI readers may call
    ``query_history`` from a worker thread.

    The schema is idempotent: ``CREATE TABLE IF NOT EXISTS`` runs on
    every ``__init__``. Multiple store instances pointing at the same
    file are fine (each holds its own connection).

    Lifecycle:
        - ``record_decision(record)`` → INSERT OR REPLACE by
          ``decision_id``. Publish writes ``decision="pending"``;
          answer/timeout write the same id again with the new state.
        - ``query_history(session_id=None, limit=100)`` → SELECT
          ordered by ``ts DESC``. Optional session filter.
        - ``close()`` → close the connection. Idempotent.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS elicitation_decisions (
        decision_id      TEXT PRIMARY KEY,
        session_id       TEXT NOT NULL,
        request_id       TEXT,
        question_id      TEXT NOT NULL,
        question_preview TEXT NOT NULL,
        options_json     TEXT NOT NULL,
        default_answer   TEXT NOT NULL,
        decision         TEXT NOT NULL,
        answer           TEXT,
        source           TEXT,
        latency_ms       INTEGER NOT NULL,
        ts               REAL NOT NULL
    );
    """

    _INDEX = (
        "CREATE INDEX IF NOT EXISTS idx_elicitation_session_ts "
        "ON elicitation_decisions(session_id, ts DESC);"
    )

    def __init__(self, db_path: Path | str) -> None:
        """Open (or create) the SQLite DB and ensure the table exists.

        Args:
            db_path: Path to the SQLite file. Parent directories are
                created automatically (mirrors ``JobStore`` /
                ``ScratchpadStore`` behaviour).
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` because the broker writes from
        # the asyncio loop thread while HTTP handlers may read from
        # Starlette's threadpool. Writes are serialised by ``_lock``.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit — each statement is its own tx
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(self._SCHEMA)
            self._conn.execute(self._INDEX)

    @property
    def db_path(self) -> Path:
        """Path of the backing SQLite file (for diagnostics)."""
        return self._db_path

    def record_decision(self, record: ElicitationDecisionRecord) -> None:
        """Upsert a decision row by ``decision_id``.

        Uses ``INSERT OR REPLACE`` so the publish-time ``pending`` row
        is atomically overwritten when the answer/timeout lands. This
        keeps the table free of orphans even if the broker process
        crashes between publish and resolve.

        Args:
            record: Fully-populated :class:`ElicitationDecisionRecord`.
        """
        options_json = json.dumps(record.options, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO elicitation_decisions (
                    decision_id, session_id, request_id, question_id,
                    question_preview, options_json, default_answer,
                    decision, answer, source, latency_ms, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.decision_id,
                    record.session_id,
                    record.request_id,
                    record.question_id,
                    record.question_preview,
                    options_json,
                    record.default_answer,
                    record.decision,
                    record.answer,
                    record.source,
                    int(record.latency_ms),
                    float(record.ts),
                ),
            )

    def query_history(
        self,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[ElicitationDecisionRecord]:
        """Return recent decisions, newest first.

        Args:
            session_id: Optional session filter (exact match).
            limit: Max rows to return (1..10_000; clamped).
        """
        limit = max(1, min(int(limit), 10_000))
        sql = (
            "SELECT decision_id, session_id, request_id, question_id, "
            "       question_preview, options_json, default_answer, "
            "       decision, answer, source, latency_ms, ts "
            "FROM elicitation_decisions"
        )
        params: list[Any] = []
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: list[ElicitationDecisionRecord] = []
        for r in rows:
            try:
                options = json.loads(r["options_json"])
                if not isinstance(options, list):
                    options = []
            except (json.JSONDecodeError, TypeError):
                options = []
            out.append(
                ElicitationDecisionRecord(
                    decision_id=r["decision_id"],
                    session_id=r["session_id"],
                    request_id=r["request_id"],
                    question_id=r["question_id"],
                    question_preview=r["question_preview"],
                    options=options,
                    default_answer=r["default_answer"],
                    decision=r["decision"],
                    answer=r["answer"],
                    source=r["source"],
                    latency_ms=r["latency_ms"],
                    ts=r["ts"],
                )
            )
        return out

    def close(self) -> None:
        """Close the SQLite connection. Safe to call multiple times."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


class ElicitationBroker:
    """In-memory pub/sub for Elicitation questions.

    Lifecycle:
        1. ``publish(question, options, default, timeout_s)`` creates a
           ``PendingQuestion``, stores it in ``_pending`` keyed by
           ``question_id``, and returns the id.
        2. A WebSocket client (or any consumer) subscribes to the broker
           (e.g. via ``pending()`` snapshot) and sends the question to
           the user.
        3. When the user answers, the client calls ``answer(question_id, value)``,
           which resolves the future.
        4. The original ``publish()`` caller awaits the future and gets
           the user's answer.
        5. If no answer arrives within ``timeout_s``, ``publish()`` falls
           back to ``default_answer`` and resolves the future with it.

    Phase 4.8 v1.18.0: an optional ``ElicitationDecisionStore`` may be
    supplied at construction time (or via ``attach_decision_store``) to
    persist every publish/answer/timeout as an
    :class:`ElicitationDecisionRecord`. Recording is best-effort: a
    SQLite error is logged and swallowed so the broker keeps working
    even if the audit DB is on a dead volume.

    Thread-safety: single asyncio loop. The broker is NOT thread-safe
    across event loops. The decision store, however, IS thread-safe
    (internal ``threading.Lock``).

    Example::

        broker = ElicitationBroker.get()
        qid = broker.publish(
            question="Run rm -rf /tmp/foo?",
            options=["proceed", "abort"],
            default_answer="abort",
            timeout_s=10.0,
        )
        answer = await broker.wait(qid)
    """

    _instance: "ElicitationBroker | None" = None

    @classmethod
    def get(cls) -> "ElicitationBroker":
        """Return the process-level singleton (lazy init)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for tests)."""
        cls._instance = None

    def __init__(
        self,
        decision_store: "ElicitationDecisionStore | None" = None,
    ) -> None:
        """Initialise the broker.

        Args:
            decision_store: Optional persistent decision history. When
                ``None`` the broker runs in v1.12.0 mode (counters
                only, no SQLite writes).
        """
        self._pending: dict[str, PendingQuestion] = {}
        self._lock = asyncio.Lock()
        self._published_total = 0
        self._answered_total = 0
        self._timed_out_total = 0
        self._decision_store = decision_store
        # Track publish wall-clock times per question_id so ``wait()``
        # can compute latency_ms without keeping the whole PendingQuestion
        # alive after the finally block pops it. Keyed by question_id.
        self._publish_ts: dict[str, float] = {}
        # Track the transport that delivered the answer (``ws`` vs
        # ``poll``). ``answer()`` records this; ``wait()`` reads it.
        self._answer_source: dict[str, str] = {}

    def attach_decision_store(
        self, store: "ElicitationDecisionStore | None",
    ) -> None:
        """Swap the decision store at runtime.

        ``None`` disables persistence (useful in tests). The previous
        store is NOT closed — the caller owns its lifecycle.
        """
        self._decision_store = store

    @property
    def decision_store(self) -> "ElicitationDecisionStore | None":
        """The currently-attached decision store (read-only view)."""
        return self._decision_store

    # === Public API ===

    def pending(self) -> list[PendingQuestion]:
        """Snapshot of currently pending questions (for WS polling)."""
        return list(self._pending.values())

    def publish(
        self,
        *,
        question: str,
        options: list[str] | None = None,
        default_answer: str = "abort",
        timeout_s: float = 30.0,
        session_id: str = "",
        request_id: str | None = None,
    ) -> str:
        """Publish a new question. Returns the question_id.

        The caller is expected to ``await broker.wait(question_id)`` to
        retrieve the answer (or the default after timeout).

        Phase 4.8: when a decision store is attached, the publish is
        recorded as a ``pending`` decision row. ``session_id`` and
        ``request_id`` are forwarded for later filtering / correlation.
        """
        qid = uuid.uuid4().hex[:12]
        now_mono = time.monotonic()
        now_wall = time.time()
        pq = PendingQuestion(
            question_id=qid,
            question=question,
            options=list(options or []),
            default_answer=default_answer,
            created_at=now_mono,
            timeout_s=timeout_s,
            session_id=session_id,
            request_id=request_id,
        )
        # asyncio dict assignment is atomic under the GIL; we use a lock
        # only to make pending() snapshots consistent.
        self._pending[qid] = pq
        self._publish_ts[qid] = now_wall
        self._published_total += 1
        logger.debug("ElicitationBroker: published qid=%s q=%r", qid, question[:60])

        # Phase 4.8: best-effort audit row.
        if self._decision_store is not None:
            try:
                self._decision_store.record_decision(
                    ElicitationDecisionRecord(
                        decision_id=qid,
                        session_id=session_id,
                        request_id=request_id,
                        question_id=qid,
                        question_preview=question[:120],
                        options=list(options or []),
                        default_answer=default_answer,
                        decision="pending",
                        answer=None,
                        source=None,
                        latency_ms=0,
                        ts=now_wall,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "ElicitationBroker: decision_store.record_decision "
                    "(pending) failed: %s", exc,
                )
        return qid

    async def wait(self, question_id: str) -> str:
        """Await the user's answer for ``question_id``.

        Returns the answer (user-supplied) or the default (on timeout).
        Always resolves exactly once.

        Phase 4.8: updates the decision row to ``answered`` (source
        ``ws`` or ``poll``, taken from :meth:`answer`'s ``source``
        argument) or ``timed_out`` (source ``timeout``).
        """
        async with self._lock:
            pq = self._pending.get(question_id)
        if pq is None:
            raise KeyError(f"unknown question_id: {question_id!r}")
        publish_ts = self._publish_ts.get(question_id, time.time())
        try:
            future = pq.resolve_future()
            answer = await asyncio.wait_for(future, timeout=pq.timeout_s)
            self._answered_total += 1
            self._record_resolve(
                question_id=question_id,
                pq=pq,
                publish_ts=publish_ts,
                decision="answered",
                answer=answer,
                source=self._answer_source.pop(question_id, "ws"),
            )
            return answer
        except asyncio.TimeoutError:
            self._timed_out_total += 1
            logger.debug(
                "ElicitationBroker: qid=%s timed out, returning default %r",
                question_id, pq.default_answer,
            )
            self._record_resolve(
                question_id=question_id,
                pq=pq,
                publish_ts=publish_ts,
                decision="timed_out",
                answer=pq.default_answer,
                source="timeout",
            )
            return pq.default_answer
        finally:
            # Best-effort cleanup; if the future was already resolved by
            # answer() we still want to remove the entry.
            self._pending.pop(question_id, None)
            self._publish_ts.pop(question_id, None)
            self._answer_source.pop(question_id, None)

    def answer(
        self,
        question_id: str,
        value: str,
        *,
        source: str = "ws",
    ) -> bool:
        """Resolve a pending question with the user's answer.

        Returns True if the question was found and resolved, False otherwise.
        Safe to call after timeout (no-op).

        Phase 4.8: ``source`` is stashed so ``wait()`` can attribute the
        resolve to ``ws`` (WebSocket) or ``poll`` (HTTP long-poll). The
        value is only consumed by ``wait()`` when it records the final
        decision row.
        """
        pq = self._pending.get(question_id)
        if pq is None:
            return False
        if pq.future is None or pq.future.done():
            return False
        pq.future.set_result(value)
        self._answer_source[question_id] = source
        logger.debug(
            "ElicitationBroker: qid=%s answered with %r (source=%s)",
            question_id, value, source,
        )
        return True

    # === Observability ===

    def stats(self) -> dict[str, int]:
        """Snapshot of broker counters (for /metrics or tests)."""
        return {
            "published_total": self._published_total,
            "answered_total": self._answered_total,
            "timed_out_total": self._timed_out_total,
            "pending_count": len(self._pending),
        }

    # === Phase 4.8 internals ===

    def _record_resolve(
        self,
        *,
        question_id: str,
        pq: PendingQuestion,
        publish_ts: float,
        decision: str,
        answer: str,
        source: str,
    ) -> None:
        """Upsert the final decision row (answered / timed_out).

        Best-effort: any SQLite error is logged and swallowed so the
        broker keeps working even when the audit DB is unavailable.
        """
        store = self._decision_store
        if store is None:
            return
        now_wall = time.time()
        latency_ms = max(0, int((now_wall - publish_ts) * 1000))
        try:
            store.record_decision(
                ElicitationDecisionRecord(
                    decision_id=question_id,
                    session_id=pq.session_id,
                    request_id=pq.request_id,
                    question_id=question_id,
                    question_preview=pq.question[:120],
                    options=list(pq.options or []),
                    default_answer=pq.default_answer,
                    decision=decision,
                    answer=answer,
                    source=source,
                    latency_ms=latency_ms,
                    ts=now_wall,
                )
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "ElicitationBroker: decision_store.record_decision "
                "(resolve=%s) failed: %s", decision, exc,
            )


__all__ = [
    "ElicitationBroker",
    "PendingQuestion",
    "ElicitationDecisionRecord",
    "ElicitationDecisionStore",
]
