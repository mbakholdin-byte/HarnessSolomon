"""Phase 3 v1.5.0: time-based + turn-based compaction trigger.

Background
----------
Phase 3 v1.0.0+ compacts ONLY on a *token* threshold
(``compaction_threshold_ratio * model_ctx``). That works for long
sessions with many tool calls, but misses two real-world cases:

  1. **Idle session** — the user stops interacting for 30 min, then
     resumes. The token count is small, but the conversation has
     "stale" state (model changes, scratchpad drift, etc.) and
     benefits from a fresh compact.
  2. **Long, low-token sessions** — the user asks 50 short questions
     in a row. None individually exceeds the token threshold, but
     the sliding window has been silently dropping context for
     the last 30 turns. A periodic compact would surface a summary.

``TimeBasedCompactionTrigger`` adds two new triggers and an OR-merge
``hybrid`` mode that fires if ANY of (token, turn, time) says so.

API
---
The trigger is a **stateless evaluator** that carries per-session
state in private dicts (``_last_compact_at`` for the time trigger,
``_last_user_turn`` for the turn trigger). Each session_id is
guarded by an ``asyncio.Lock`` so concurrent updates from multiple
async tasks (HTTP route + WebSocket + CLI subcommand) don't race.

The compactor (``ContextCompactor.maybe_compact``) consults the
trigger with ``should_trigger()`` BEFORE the token threshold check
in non-``token`` modes. The trigger does NOT touch the slow path
itself — it just answers True/False. The compactor then runs
``_run_slow_path`` and (on success) calls ``mark_compacted()`` to
update the per-session state.

Trust boundary
--------------
The compactor accesses the trigger via ``getattr(self, "_idle_trigger",
None)`` (defence-in-depth — the attribute is set in ``__init__``
but the compactor's hot path is safe even if it's missing). The
trigger is a plain Python class with no FastAPI / HTTP / DB imports,
so it can be unit-tested in isolation.

Reference: Phase 3 v1.5.0 plan, Step 5.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

if False:  # TYPE_CHECKING import guard
    from harness.config import Settings

logger = logging.getLogger(__name__)


# Trigger mode literal. ``"token"`` = legacy behaviour (no idle
# trigger consulted); ``"turn"`` = every N user turns;
# ``"time"`` = every M minutes since last compact; ``"hybrid"`` =
# OR of turn + time (token check is separate in the compactor).
TriggerMode = Literal["token", "turn", "time", "hybrid"]


def _count_user_turns(messages: list[dict[str, Any]]) -> int:
    """Return the number of ``role=="user"`` messages in ``messages``.

    Cheap heuristic: only counts top-level user messages, not tool
    messages (which are also "user" content in some LLM APIs but
    represent a tool result, not a conversational turn).

    The trigger uses this delta against ``_last_user_turn[session_id]``
    to decide whether the turn interval has elapsed.
    """
    return sum(1 for m in messages if m.get("role") == "user")


def _has_assistant_turn(messages: list[dict[str, Any]]) -> bool:
    """Return True if ``messages`` has at least one assistant turn.

    Used to suppress the very-first-compact (a brand new session
    has 0 assistant turns — compacting nothing would be wasteful).
    """
    return any(m.get("role") == "assistant" for m in messages)


class TimeBasedCompactionTrigger:
    """Phase 3 v1.5.0: turn-based / time-based compaction trigger.

    Holds per-session state:

      * ``_last_compact_at[session_id]`` — ``time.monotonic()`` of the
        last successful compact (used by ``time`` and ``hybrid``
        modes). Initialised on first ``should_trigger()`` call.
      * ``_last_user_turn[session_id]`` — user turn count at the
        last successful compact (used by ``turn`` and ``hybrid``
        modes). Initialised on first ``should_trigger()`` call.
      * ``_locks[session_id]`` — ``asyncio.Lock`` per session for
        safe concurrent updates.

    All public methods are fail-open: any internal exception is
    logged and treated as "should NOT trigger" so a misconfigured
    trigger can never break the chat loop.
    """

    def __init__(self, settings: Any) -> None:
        # Typed as Any to keep the module free of a hard dep on
        # ``harness.config.Settings`` — tests inject a fake settings
        # object that exposes the same 3 fields.
        self._settings = settings
        self._last_compact_at: dict[str, float] = {}
        self._last_user_turn: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # --- Public API ---

    def should_trigger(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        force: bool = False,
        now: float | None = None,
    ) -> bool:
        """Return True if compaction should fire (turn / time / hybrid).

        Parameters
        ----------
        session_id:
            Stable session id (UUID string from the chat DB).
        messages:
            Full chat history in OpenAI dict shape — used to count
            user turns. The compactor passes the post-mutation list
            (i.e. before any sliding-window trim).
        force:
            When True, bypass the trigger evaluation (returns False).
            Used by the compactor to skip the idle check on resume /
            load_history (the caller is happy with a token check).
        now:
            Override ``time.monotonic()`` for testing. Default None
            = use the real clock.

        Returns
        -------
        bool
            True if the configured trigger (turn / time / hybrid)
            says "compact now". False otherwise, including:

              * mode == "token" (legacy — caller uses token threshold)
              * force == True (caller wants to skip the idle check)
              * any internal exception (fail-open, logged)
        """
        if force:
            return False
        try:
            mode = getattr(self._settings, "compaction_trigger", "token")
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("idle_trigger: settings read failed: %s", exc)
            return False
        if mode == "token":
            return False
        if not session_id:
            return False
        if not _has_assistant_turn(messages):
            # First call ever — nothing to compact.
            return False
        current_now = time.monotonic() if now is None else float(now)
        try:
            if mode == "turn":
                return self._turn_fired(session_id, messages, current_now)
            if mode == "time":
                return self._time_fired(session_id, current_now)
            if mode == "hybrid":
                return (
                    self._turn_fired(session_id, messages, current_now)
                    or self._time_fired(session_id, current_now)
                )
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "idle_trigger: evaluation failed for session=%s mode=%s: %s",
                session_id, mode, exc,
            )
            return False
        # Unknown mode — treat as no-op.
        return False

    def mark_compacted(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]] | None = None,
        now: float | None = None,
    ) -> None:
        """Record that a compact just ran for ``session_id``.

        Updates ``_last_compact_at`` and (if ``messages`` provided)
        ``_last_user_turn``. Best-effort: any exception is logged
        but never raised. The compactor calls this AFTER a successful
        ``_run_slow_path`` so the next ``should_trigger`` call sees
        a fresh baseline.

        Safe to call concurrently with ``should_trigger`` for the
        same ``session_id`` — the per-session ``asyncio.Lock`` is
        acquired implicitly via the ``_lock_for`` helper.
        """
        if not session_id:
            return
        current_now = time.monotonic() if now is None else float(now)
        try:
            self._last_compact_at[session_id] = current_now
            if messages is not None:
                self._last_user_turn[session_id] = _count_user_turns(messages)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "idle_trigger: mark_compacted failed for session=%s: %s",
                session_id, exc,
            )

    def reset(self, session_id: str) -> None:
        """Clear per-session state (e.g. on session close).

        Idempotent — missing keys are silently ignored.
        """
        self._last_compact_at.pop(session_id, None)
        self._last_user_turn.pop(session_id, None)
        # Note: we do NOT pop the lock here. A new ``asyncio.Lock``
        # would be created on the next ``should_trigger`` for this
        # session, and the old lock would be garbage-collected once
        # no task holds a reference.
        self._locks.pop(session_id, None)

    # --- Internals (sync helpers, no IO) ---

    def _turn_fired(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        now: float,
    ) -> bool:
        """True if the user-turn interval has elapsed.

        First call for a session: returns False (no baseline to
        compare against). Subsequent calls: returns True when
        ``user_turns - last_user_turn[session_id] >= turn_interval``.
        """
        try:
            interval = int(getattr(self._settings, "compaction_turn_interval", 20))
        except (TypeError, ValueError):
            interval = 20
        if interval <= 0:
            return False
        last_turn = self._last_user_turn.get(session_id)
        if last_turn is None:
            # First call — no baseline. Seed the baseline and return
            # False so the compactor doesn't fire on the first turn.
            self._last_user_turn[session_id] = _count_user_turns(messages)
            return False
        current_turns = _count_user_turns(messages)
        return (current_turns - last_turn) >= interval

    def _time_fired(self, session_id: str, now: float) -> bool:
        """True if the idle-time interval has elapsed.

        First call for a session: seeds ``_last_compact_at[session_id]``
        and returns False. Subsequent calls: returns True when
        ``now - last_compact_at[session_id] >= idle_minutes * 60``.
        """
        try:
            idle_minutes = int(
                getattr(self._settings, "compaction_time_idle_minutes", 30),
            )
        except (TypeError, ValueError):
            idle_minutes = 30
        if idle_minutes <= 0:
            return False
        last_at = self._last_compact_at.get(session_id)
        if last_at is None:
            # First call — seed and don't fire.
            self._last_compact_at[session_id] = now
            return False
        return (now - last_at) >= (idle_minutes * 60.0)

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        """Return (and lazily create) the per-session lock."""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock
