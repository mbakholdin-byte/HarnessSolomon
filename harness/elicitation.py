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

Trust boundary: this module is stdlib + asyncio + dataclasses only. No
``harness.agents`` / ``harness.server`` / ``harness.hooks`` imports.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
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
    """

    question_id: str
    question: str
    options: list[str]
    default_answer: str
    future: asyncio.Future[str] | None = None
    created_at: float = 0.0
    timeout_s: float = 30.0

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

    Thread-safety: single asyncio loop. The broker is NOT thread-safe
    across event loops.

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

    def __init__(self) -> None:
        self._pending: dict[str, PendingQuestion] = {}
        self._lock = asyncio.Lock()
        self._published_total = 0
        self._answered_total = 0
        self._timed_out_total = 0

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
    ) -> str:
        """Publish a new question. Returns the question_id.

        The caller is expected to ``await broker.wait(question_id)`` to
        retrieve the answer (or the default after timeout).
        """
        import time as _time

        qid = uuid.uuid4().hex[:12]
        pq = PendingQuestion(
            question_id=qid,
            question=question,
            options=list(options or []),
            default_answer=default_answer,
            created_at=_time.monotonic(),
            timeout_s=timeout_s,
        )
        # asyncio dict assignment is atomic under the GIL; we use a lock
        # only to make pending() snapshots consistent.
        self._pending[qid] = pq
        self._published_total += 1
        logger.debug("ElicitationBroker: published qid=%s q=%r", qid, question[:60])
        return qid

    async def wait(self, question_id: str) -> str:
        """Await the user's answer for ``question_id``.

        Returns the answer (user-supplied) or the default (on timeout).
        Always resolves exactly once.
        """
        async with self._lock:
            pq = self._pending.get(question_id)
        if pq is None:
            raise KeyError(f"unknown question_id: {question_id!r}")
        try:
            future = pq.resolve_future()
            answer = await asyncio.wait_for(future, timeout=pq.timeout_s)
            self._answered_total += 1
            return answer
        except asyncio.TimeoutError:
            self._timed_out_total += 1
            logger.debug(
                "ElicitationBroker: qid=%s timed out, returning default %r",
                question_id, pq.default_answer,
            )
            return pq.default_answer
        finally:
            # Best-effort cleanup; if the future was already resolved by
            # answer() we still want to remove the entry.
            self._pending.pop(question_id, None)

    def answer(self, question_id: str, value: str) -> bool:
        """Resolve a pending question with the user's answer.

        Returns True if the question was found and resolved, False otherwise.
        Safe to call after timeout (no-op).
        """
        pq = self._pending.get(question_id)
        if pq is None:
            return False
        if pq.future is None or pq.future.done():
            return False
        pq.future.set_result(value)
        logger.debug("ElicitationBroker: qid=%s answered with %r", question_id, value)
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


__all__ = ["ElicitationBroker", "PendingQuestion"]
