"""Phase 3 v1.5.0 Step 5: tests for TimeBasedCompactionTrigger.

Covers:
- Turn mode (every N user turns) — fires / doesn't fire
- Time mode (after M idle minutes) — fires / doesn't fire
- Hybrid mode (OR of turn + time)
- Token mode (default) — always returns False (legacy behaviour)
- Disabled (mode="token" or force=True) — never fires
- Fail-open: settings read error, mark_compacted error → no crash
- mark_compacted updates per-session state
- Per-session isolation: two sessions have independent state
- First call seeds baseline (does NOT fire)
- Reset clears state
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from harness.agents.idle_trigger import TimeBasedCompactionTrigger


# --- Fixtures ---


class _FakeSettings:
    """Settings stub with controllable trigger mode + intervals."""

    def __init__(
        self,
        mode: str = "token",
        turn_interval: int = 20,
        idle_minutes: int = 30,
    ) -> None:
        self.compaction_trigger = mode
        self.compaction_turn_interval = turn_interval
        self.compaction_time_idle_minutes = idle_minutes


def _msgs_with(n_user: int, n_assistant: int = 0) -> list[dict[str, Any]]:
    """Build a synthetic message list with N user + M assistant turns."""
    out: list[dict[str, Any]] = []
    for i in range(n_user):
        out.append({"role": "user", "content": f"q{i}"})
    for i in range(n_assistant):
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


# --- Test 1: Token mode (default) ---


class TestTokenMode:
    """``compaction_trigger == "token"`` → trigger never fires (legacy)."""

    def test_token_mode_returns_false(self) -> None:
        """Default mode = token → should_trigger always returns False."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="token"),
        )
        # Even with 100 user turns and 999 days idle, the trigger
        # must not fire — the compactor uses its own token check.
        result = trig.should_trigger(
            session_id="s1",
            messages=_msgs_with(100, 100),
        )
        assert result is False

    def test_empty_messages_returns_false(self) -> None:
        """No messages → no assistant turn → first-call guard returns False."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn"),
        )
        assert trig.should_trigger(
            session_id="s1", messages=[],
        ) is False

    def test_first_user_turn_only_returns_false(self) -> None:
        """User msg but no assistant turn → guard returns False (nothing to compact)."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn"),
        )
        assert trig.should_trigger(
            session_id="s1",
            messages=_msgs_with(5, 0),  # 5 user, 0 assistant
        ) is False


# --- Test 2: Turn mode ---


class TestTurnMode:
    """``compaction_trigger == "turn"`` → fires every N user turns."""

    def test_turn_fires_when_interval_exceeded(self) -> None:
        """After 21 user turns since last compact → fires."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn", turn_interval=20),
        )
        # Seed: first call with 5 user turns → seeds baseline, returns False.
        msgs_a = _msgs_with(5, 5)
        assert trig.should_trigger(
            session_id="s1", messages=msgs_a,
        ) is False
        # Mark compacted at this point.
        trig.mark_compacted(session_id="s1", messages=msgs_a)
        # Now advance by 20 user turns (interval = 20).
        msgs_b = _msgs_with(25, 25)  # 20 more user turns
        # 25 - 5 = 20 turns since last compact = interval. Borderline:
        # our impl uses ``>=``, so this should fire.
        assert trig.should_trigger(
            session_id="s1", messages=msgs_b,
        ) is True

    def test_turn_does_not_fire_under_interval(self) -> None:
        """Below interval → does not fire."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn", turn_interval=20),
        )
        msgs_a = _msgs_with(5, 5)
        trig.should_trigger(session_id="s1", messages=msgs_a)  # seed
        trig.mark_compacted(session_id="s1", messages=msgs_a)
        # 10 more turns → 10 < 20, should not fire.
        msgs_b = _msgs_with(15, 15)
        assert trig.should_trigger(
            session_id="s1", messages=msgs_b,
        ) is False

    def test_turn_with_zero_interval_disables(self) -> None:
        """``turn_interval <= 0`` → trigger is disabled (returns False)."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn", turn_interval=0),
        )
        msgs_a = _msgs_with(5, 5)
        trig.should_trigger(session_id="s1", messages=msgs_a)  # seed
        trig.mark_compacted(session_id="s1", messages=msgs_a)
        # 100 more turns but interval is 0 → no fire.
        msgs_b = _msgs_with(105, 105)
        assert trig.should_trigger(
            session_id="s1", messages=msgs_b,
        ) is False


# --- Test 3: Time mode ---


class TestTimeMode:
    """``compaction_trigger == "time"`` → fires after M idle minutes."""

    def test_time_fires_after_idle_minutes(self) -> None:
        """After 31 minutes since last compact → fires."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="time", idle_minutes=30),
        )
        msgs = _msgs_with(5, 5)
        # First call: seeds last_compact_at, returns False.
        t0 = 1_000.0
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t0,
        ) is False
        # 30 min + 1 sec later → 30*60 + 1 = 1801 sec, > 1800.
        t1 = t0 + 1801.0
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t1,
        ) is True

    def test_time_does_not_fire_within_idle_window(self) -> None:
        """Within idle window → does not fire."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="time", idle_minutes=30),
        )
        msgs = _msgs_with(5, 5)
        t0 = 1_000.0
        trig.should_trigger(session_id="s1", messages=msgs, now=t0)
        # 10 minutes later = 600 sec, < 1800.
        t1 = t0 + 600.0
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t1,
        ) is False

    def test_time_with_zero_idle_disables(self) -> None:
        """``idle_minutes <= 0`` → trigger disabled."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="time", idle_minutes=0),
        )
        msgs = _msgs_with(5, 5)
        t0 = 1_000.0
        trig.should_trigger(session_id="s1", messages=msgs, now=t0)
        # 1 second later → not 0 minutes, but interval=0 short-circuits.
        t1 = t0 + 1.0
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t1,
        ) is False


# --- Test 4: Hybrid mode ---


class TestHybridMode:
    """``compaction_trigger == "hybrid"`` → OR of turn + time."""

    def test_hybrid_fires_on_turn(self) -> None:
        """Hybrid fires when turn interval elapses (time still under)."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="hybrid", turn_interval=10, idle_minutes=30),
        )
        msgs_a = _msgs_with(5, 5)
        t0 = 1_000.0
        trig.should_trigger(session_id="s1", messages=msgs_a, now=t0)  # seed
        trig.mark_compacted(session_id="s1", messages=msgs_a)
        # 15 turns later (>= 10 interval) but only 5 minutes later
        # (< 30 idle_minutes) → turn fires.
        msgs_b = _msgs_with(20, 20)
        t1 = t0 + 300.0  # 5 min
        assert trig.should_trigger(
            session_id="s1", messages=msgs_b, now=t1,
        ) is True

    def test_hybrid_fires_on_time(self) -> None:
        """Hybrid fires when idle minutes elapse (turn still under)."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="hybrid", turn_interval=100, idle_minutes=30),
        )
        msgs = _msgs_with(5, 5)
        t0 = 1_000.0
        trig.should_trigger(session_id="s1", messages=msgs, now=t0)  # seeds _last_compact_at=t0
        # 2 more user turns (< 100 interval) but 31 minutes later
        # → time fires.
        msgs_b = _msgs_with(7, 7)
        t1 = t0 + 1860.0  # 31 min
        assert trig.should_trigger(
            session_id="s1", messages=msgs_b, now=t1,
        ) is True

    def test_hybrid_no_fire_when_neither(self) -> None:
        """Both turn and time under threshold → does not fire."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="hybrid", turn_interval=100, idle_minutes=30),
        )
        msgs = _msgs_with(5, 5)
        t0 = 1_000.0
        trig.should_trigger(session_id="s1", messages=msgs, now=t0)  # seed
        trig.mark_compacted(session_id="s1", messages=msgs)
        # 2 more turns (< 100), 5 minutes (< 30) → no fire.
        msgs_b = _msgs_with(7, 7)
        t1 = t0 + 300.0  # 5 min
        assert trig.should_trigger(
            session_id="s1", messages=msgs_b, now=t1,
        ) is False


# --- Test 5: Force / disabled / per-session isolation ---


class TestForceAndIsolation:
    """force=True skips the trigger; sessions have independent state."""

    def test_force_true_returns_false(self) -> None:
        """``force=True`` (resume path) → trigger skipped."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="time", idle_minutes=30),
        )
        msgs = _msgs_with(100, 100)
        t0 = 1_000.0
        # Without force, time mode would fire after 30 min.
        # With force=True, must return False even if the trigger
        # would otherwise say "compact now".
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t0 + 9999.0, force=True,
        ) is False

    def test_per_session_isolation(self) -> None:
        """s1 fires, s2 does not — independent state."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="time", idle_minutes=30),
        )
        msgs = _msgs_with(5, 5)
        t0 = 1_000.0
        # Seed s1 only — s2 has no baseline.
        trig.should_trigger(session_id="s1", messages=msgs, now=t0)
        # 31 minutes later: s1 last_compact_at = t0, so 31 min elapsed
        # → s1 fires.
        t1 = t0 + 1861.0
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t1,
        ) is True
        # s2 has no baseline → first call seeds and returns False.
        # This proves s1's state change didn't affect s2.
        assert trig.should_trigger(
            session_id="s2", messages=msgs, now=t1,
        ) is False

    def test_empty_session_id_returns_false(self) -> None:
        """Empty session_id → trigger returns False (no state)."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn"),
        )
        msgs = _msgs_with(100, 100)
        assert trig.should_trigger(
            session_id="", messages=msgs,
        ) is False

    def test_reset_clears_per_session_state(self) -> None:
        """After reset, the session is back to "no baseline"."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="time", idle_minutes=30),
        )
        msgs = _msgs_with(5, 5)
        t0 = 1_000.0
        # Seed.
        trig.should_trigger(session_id="s1", messages=msgs, now=t0)
        # 31 min later: would normally fire.
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t0 + 1861.0,
        ) is True
        # Reset and re-check: first call after reset should seed
        # and return False (no baseline).
        trig.reset("s1")
        assert trig.should_trigger(
            session_id="s1", messages=msgs, now=t0 + 1861.0,
        ) is False


# --- Test 6: Fail-open ---


class TestFailOpen:
    """Any internal exception → trigger returns False (no crash)."""

    def test_settings_read_failure_returns_false(self) -> None:
        """``getattr`` raises → trigger returns False, logs warning."""

        class _BrokenSettings:
            @property
            def compaction_trigger(self) -> str:
                raise RuntimeError("settings broken")

        trig = TimeBasedCompactionTrigger(settings=_BrokenSettings())
        # Must not raise.
        result = trig.should_trigger(
            session_id="s1",
            messages=_msgs_with(100, 100),
        )
        assert result is False

    def test_mark_compacted_with_broken_settings_does_not_raise(self) -> None:
        """mark_compacted is best-effort — never raises."""

        class _BrokenSettings:
            pass  # no attributes

        trig = TimeBasedCompactionTrigger(settings=_BrokenSettings())
        # Must not raise (no attributes to read on mark_compacted).
        trig.mark_compacted(session_id="s1", messages=_msgs_with(5, 5))

    def test_mark_compacted_empty_session_id_no_op(self) -> None:
        """Empty session_id → mark_compacted is a no-op (early return)."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn"),
        )
        # Must not raise, must not seed state.
        trig.mark_compacted(session_id="", messages=_msgs_with(5, 5))
        # State dicts should be empty.
        assert trig._last_compact_at == {}
        assert trig._last_user_turn == {}


# --- Test 7: mark_compacted state updates ---


class TestMarkCompacted:
    """``mark_compacted`` updates _last_compact_at and _last_user_turn."""

    def test_mark_compacted_records_user_turn_count(self) -> None:
        """mark_compacted(messages=...) updates _last_user_turn."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn", turn_interval=10),
        )
        msgs = _msgs_with(7, 7)  # 7 user turns
        trig.mark_compacted(session_id="s1", messages=msgs)
        assert trig._last_user_turn["s1"] == 7

    def test_mark_compacted_records_compact_time(self) -> None:
        """mark_compacted updates _last_compact_at to current time."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="time"),
        )
        fixed_now = 12_345.0
        trig.mark_compacted(session_id="s1", messages=_msgs_with(1, 1), now=fixed_now)
        assert trig._last_compact_at["s1"] == fixed_now

    def test_mark_compacted_without_messages_only_updates_time(self) -> None:
        """mark_compacted(messages=None) → only _last_compact_at is set."""
        trig = TimeBasedCompactionTrigger(
            settings=_FakeSettings(mode="turn"),
        )
        trig.mark_compacted(session_id="s1", messages=None, now=999.0)
        assert trig._last_compact_at["s1"] == 999.0
        assert "s1" not in trig._last_user_turn  # unchanged
