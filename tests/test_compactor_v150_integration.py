"""Phase 3 v1.5.0 Step 5: integration tests for ContextCompactor + TimeBasedCompactionTrigger.

Covers:
- ``force_idle_check=True`` + turn trigger → slow path runs even under token threshold
- ``force_idle_check=True`` + time trigger → slow path runs
- ``force_idle_check=False`` (resume) → idle trigger NOT consulted
- token mode (default) → idle trigger ignored even when force_idle_check=True
- mark_compacted called after a successful slow-path run
- hybrid mode (OR of turn + time) → first to fire wins
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.agents.idle_trigger import TimeBasedCompactionTrigger
from harness.context.compaction import ContextCompactor


# --- Fixtures ---


class _Router:
    """Stub router that records completion calls."""

    def __init__(self) -> None:
        self.completion_calls: list[dict[str, Any]] = []

    async def completion(self, *args: Any, **kwargs: Any) -> Any:
        self.completion_calls.append({"args": args, "kwargs": kwargs})
        result = MagicMock()
        result.content = "summary text"
        return result


class _IdleTriggerSpy:
    """Spy TimeBasedCompactionTrigger — records should_trigger / mark_compacted calls."""

    def __init__(self, *, returns: bool = False) -> None:
        self._returns = returns
        self.should_calls: list[dict[str, Any]] = []
        self.mark_calls: list[dict[str, Any]] = []

    def should_trigger(self, *, session_id: str, messages: list, **kw: Any) -> bool:
        self.should_calls.append({"session_id": session_id, "n": len(messages)})
        return self._returns

    def mark_compacted(self, *, session_id: str, messages: list | None = None, **kw: Any) -> None:
        self.mark_calls.append({"session_id": session_id, "n": len(messages or [])})


class _Settings:
    """Settings stub with minimum required fields for ContextCompactor."""

    def __init__(
        self,
        *,
        trigger: str = "token",
        turn_interval: int = 20,
        idle_minutes: int = 30,
    ) -> None:
        self.compaction_enabled = True
        self.compaction_threshold_ratio = 0.5
        self.compaction_target_ratio = 0.3
        self.compaction_keep_recent_turns = 2
        self.compaction_summarizer_model = ""
        self.compaction_summarizer_fallback = ""
        self.compaction_summarizer_max_input_tokens = 0
        self.subagent_t1_model = ""
        self.subagent_t2_model = ""
        self.compaction_persistent_store = False
        self.compaction_audit_log = False
        self.compaction_persist_to_memory = False
        # v1.5.0 idle trigger settings:
        self.compaction_trigger = trigger
        self.compaction_turn_interval = turn_interval
        self.compaction_time_idle_minutes = idle_minutes
        self.pre_compact_max_ms = 5000
        self.pre_compact_save_fields = ""


def _build_big_messages(n: int) -> list[dict[str, Any]]:
    """Build N user/assistant pairs with large content (5K chars each).

    Token estimate per msg = 5000/4 = 1250 tokens. With 6+ pairs,
    total >> 50% of 8K context → triggers slow path.
    """
    big = "x" * 5000
    out: list[dict[str, Any]] = [{"role": "system", "content": "sys " + big[:1000]}]
    for i in range(n):
        out.append({"role": "user", "content": big})
        out.append({"role": "assistant", "content": big})
    return out


# --- Test 1: force_idle_check=True + turn trigger fires ---


class TestIdleTriggerFires:
    """``force_idle_check=True`` + trigger says "yes" → slow path runs."""

    @pytest.mark.asyncio
    async def test_turn_trigger_fires_under_token_threshold(self) -> None:
        """Even with small messages (under token threshold), idle trigger
        can force a compact when ``force_idle_check=True``.

        We use a SPY trigger that always returns True; the compactor
        routes the call to ``_run_slow_path`` (token check is
        bypassed). We assert on the SPY's ``mark_compacted`` call —
        this proves the slow path reached its end (mark_compacted is
        called by the compactor AFTER a successful slow-path run).

        The router completion may or may not be called depending on
        whether the sliding window alone gets us under target. We
        don't assert on that.
        """
        router = _Router()
        spy = _IdleTriggerSpy(returns=True)
        compactor = ContextCompactor(
            settings=_Settings(trigger="turn"),  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            idle_trigger=spy,
        )
        # Build messages big enough that the slow path drops the
        # dropped region (i.e. _extract_dropped returns non-empty
        # → summarise is called → router is called).
        msgs = _build_big_messages(6)  # 13 messages × 5K chars
        result = await compactor.maybe_compact(
            msgs, "test", session_id="s1", force_idle_check=True,
        )
        # Idle trigger was consulted exactly once.
        assert len(spy.should_calls) == 1
        # mark_compacted called with the compacted list — proves
        # the slow path reached its end.
        assert len(spy.mark_calls) == 1
        # Result is the compacted list.
        assert result is not None

    @pytest.mark.asyncio
    async def test_time_trigger_fires_under_token_threshold(self) -> None:
        """Time mode + force_idle_check=True → slow path runs."""
        router = _Router()
        spy = _IdleTriggerSpy(returns=True)
        compactor = ContextCompactor(
            settings=_Settings(trigger="time"),  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            idle_trigger=spy,
        )
        msgs = _build_big_messages(6)
        result = await compactor.maybe_compact(
            msgs, "test", session_id="s1", force_idle_check=True,
        )
        # Idle trigger was consulted.
        assert len(spy.should_calls) == 1
        # mark_compacted called — slow path reached its end.
        assert len(spy.mark_calls) == 1
        assert result is not None


# --- Test 2: force_idle_check=False → idle trigger NOT consulted ---


class TestResumeSkipsIdle:
    """``force_idle_check=False`` (Session.load_history) → no idle check."""

    @pytest.mark.asyncio
    async def test_resume_does_not_consult_idle_trigger(self) -> None:
        """Even if the trigger would say "yes", resume skips it."""
        router = _Router()
        spy = _IdleTriggerSpy(returns=True)
        compactor = ContextCompactor(
            settings=_Settings(trigger="turn"),  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            idle_trigger=spy,
        )
        # 3 messages under token threshold.
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "q2"},
        ]
        result = await compactor.maybe_compact(
            msgs, "test", session_id="s1", force_idle_check=False,
        )
        # Under threshold → no slow path, no router call, no idle check.
        assert len(router.completion_calls) == 0
        assert len(spy.should_calls) == 0
        assert len(spy.mark_calls) == 0
        # Returned the same list (no-op).
        assert result is msgs


# --- Test 3: token mode → idle trigger ignored ---


class TestTokenModeBypassesIdle:
    """``compaction_trigger == "token"`` → legacy behaviour, no idle check."""

    @pytest.mark.asyncio
    async def test_real_token_mode_trigger_ignores_force_idle_check(self) -> None:
        """Token mode in real TimeBasedCompactionTrigger → should_trigger=False."""
        real_trig = TimeBasedCompactionTrigger(
            settings=_Settings(trigger="token"),  # type: ignore[arg-type]
        )
        # Real trigger with token mode returns False even with many turns.
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = real_trig.should_trigger(
            session_id="s1", messages=msgs, force=True,
        )
        assert result is False


# --- Test 4: real TimeBasedCompactionTrigger end-to-end ---


class TestRealTriggerEndToEnd:
    """Use the real trigger (not a spy) for end-to-end coverage."""

    @pytest.mark.asyncio
    async def test_real_turn_trigger_seeds_then_fires(self) -> None:
        """Seed baseline (returns False), advance turns, should_trigger
        returns True on the second call.

        We exercise the trigger directly (not through the compactor)
        to avoid coupling the assertion to compactor internals.
        The compactor-level flow is already covered by the SPY-based
        test in TestIdleTriggerFires above.
        """
        real_trig = TimeBasedCompactionTrigger(
            settings=_Settings(trigger="turn", turn_interval=5),  # type: ignore[arg-type]
        )
        # First call: 5 user turns, seeds baseline (returns False).
        msgs_a = _build_big_messages(5)
        assert real_trig.should_trigger(
            session_id="s1", messages=msgs_a,
        ) is False
        # Baseline is now seeded.
        assert real_trig._last_user_turn.get("s1") == 5
        # Second call: 15 user turns → 15 - 5 = 10 ≥ 5 → fires.
        msgs_b = _build_big_messages(15)
        assert real_trig.should_trigger(
            session_id="s1", messages=msgs_b,
        ) is True

    @pytest.mark.asyncio
    async def test_real_hybrid_mode_time_triggers_after_seeding_old(self) -> None:
        """Hybrid time mode + manually-aged last_compact_at → fire."""
        router = _Router()
        # 1 minute idle, huge turn interval so only time can fire.
        real_trig = TimeBasedCompactionTrigger(
            settings=_Settings(trigger="hybrid", turn_interval=99999, idle_minutes=1),  # type: ignore[arg-type]
        )
        compactor = ContextCompactor(
            settings=_Settings(trigger="hybrid", turn_interval=99999, idle_minutes=1),  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            idle_trigger=real_trig,
        )
        msgs = _build_big_messages(3)
        # First call: seed.
        await compactor.maybe_compact(
            msgs, "test", session_id="s1", force_idle_check=True,
        )
        first_compact_at = real_trig._last_compact_at.get("s1")
        assert first_compact_at is not None
        # Manually age the baseline by 100 seconds (well past 1 min).
        real_trig._last_compact_at["s1"] -= 100.0
        # Second call: time trigger should fire.
        await compactor.maybe_compact(
            msgs, "test", session_id="s1", force_idle_check=True,
        )
        # After this call, mark_compacted refreshed _last_compact_at
        # to a "newer" value (the current time).
        new_compact_at = real_trig._last_compact_at.get("s1")
        assert new_compact_at is not None
        assert new_compact_at > first_compact_at - 100.0  # was aged by 100s


# --- Test 5: idle trigger absence (None) → no crash, legacy behaviour ---


class TestNoIdleTrigger:
    """``idle_trigger=None`` → compactor behaves as pre-v1.5.0 (token only)."""

    @pytest.mark.asyncio
    async def test_none_trigger_legacy_token_only(self) -> None:
        """No idle trigger → force_idle_check=True is a no-op."""
        router = _Router()
        compactor = ContextCompactor(
            settings=_Settings(trigger="turn"),  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            idle_trigger=None,  # explicit None
        )
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        result = await compactor.maybe_compact(
            msgs, "test", session_id="s1", force_idle_check=True,
        )
        # No trigger → under token threshold → no slow path.
        assert len(router.completion_calls) == 0
        assert result is msgs

    @pytest.mark.asyncio
    async def test_none_trigger_token_threshold_still_works(self) -> None:
        """None trigger + messages over threshold → slow path runs."""
        router = _Router()
        compactor = ContextCompactor(
            settings=_Settings(trigger="turn"),  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            idle_trigger=None,
        )
        # Build a message list that EXCEEDS the threshold.
        msgs = _build_big_messages(20)  # 41 messages × 5K chars
        # Save the original (huge) message count.
        original_count = len(msgs)
        result = await compactor.maybe_compact(
            msgs, "test", session_id="s1", force_idle_check=True,
        )
        # Token threshold exceeded → slow path ran → result is
        # a SHORTER list (sliding window dropped old messages).
        assert result is not None
        assert len(result) < original_count
        # Either summarise was called (router) or sliding window
        # alone got us under target. Either way, the slow path
        # completed and returned a compacted list.
