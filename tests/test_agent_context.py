"""Tests for harness.agents.context (Phase 7.6 — WI-02).

Covers:
  - AgentContext initial state (all counters = 0)
  - update_from_usage increments counters
  - get_context_size returns prompt + completion total
  - last_context_size tracks last prompt_tokens
  - reset() zeros everything
  - Session isolation (different session_id → different contexts)
  - remove_context cleans up a session
"""
from __future__ import annotations

import pytest

from harness.agents.context import (
    AgentContext,
    get_context,
    remove_context,
    update_context,
)


# === AgentContext unit tests ===


class TestAgentContextInitialState:
    """All counters start at zero."""

    def test_default_constructor(self) -> None:
        ctx = AgentContext()
        assert ctx.session_id == ""
        assert ctx.cumulative_prompt_tokens == 0
        assert ctx.cumulative_completion_tokens == 0
        assert ctx.last_context_size == 0
        assert ctx.turn_count == 0

    def test_session_id_passthrough(self) -> None:
        ctx = AgentContext(session_id="abc-123")
        assert ctx.session_id == "abc-123"
        assert ctx.cumulative_prompt_tokens == 0


class TestUpdateFromUsage:
    """update_from_usage increments counters correctly."""

    def test_increments_prompt_tokens(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=500, completion_tokens=200)
        assert ctx.cumulative_prompt_tokens == 500

    def test_increments_completion_tokens(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=500, completion_tokens=200)
        assert ctx.cumulative_completion_tokens == 200

    def test_increments_turn_count(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=500, completion_tokens=200)
        assert ctx.turn_count == 1
        ctx.update_from_usage(prompt_tokens=300, completion_tokens=100)
        assert ctx.turn_count == 2

    def test_cumulative_across_turns(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=500, completion_tokens=200)
        ctx.update_from_usage(prompt_tokens=300, completion_tokens=150)
        assert ctx.cumulative_prompt_tokens == 800
        assert ctx.cumulative_completion_tokens == 350


class TestGetContextSize:
    """get_context_size returns prompt + completion."""

    def test_initial_zero(self) -> None:
        ctx = AgentContext()
        assert ctx.get_context_size() == 0

    def test_after_update(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=1000, completion_tokens=300)
        assert ctx.get_context_size() == 1300

    def test_accumulated_across_turns(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=100, completion_tokens=50)
        ctx.update_from_usage(prompt_tokens=200, completion_tokens=80)
        assert ctx.get_context_size() == 430


class TestLastContextSize:
    """last_context_size tracks the most recent prompt_tokens."""

    def test_initial_zero(self) -> None:
        ctx = AgentContext()
        assert ctx.last_context_size == 0

    def test_equals_last_prompt_tokens(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=500, completion_tokens=200)
        assert ctx.last_context_size == 500
        ctx.update_from_usage(prompt_tokens=800, completion_tokens=300)
        assert ctx.last_context_size == 800


class TestReset:
    """reset() zeroes everything."""

    def test_reset_zeroes_counters(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=500, completion_tokens=200)
        ctx.update_from_usage(prompt_tokens=300, completion_tokens=100)
        assert ctx.cumulative_prompt_tokens > 0
        assert ctx.turn_count == 2

        ctx.reset()

        assert ctx.cumulative_prompt_tokens == 0
        assert ctx.cumulative_completion_tokens == 0
        assert ctx.last_context_size == 0
        assert ctx.turn_count == 0

    def test_reset_preserves_session_id(self) -> None:
        ctx = AgentContext(session_id="keep-me")
        ctx.update_from_usage(prompt_tokens=100, completion_tokens=50)
        ctx.reset()
        assert ctx.session_id == "keep-me"


# === Module-level session-scoped functions ===


class TestSessionIsolation:
    """Different session_id produce different AgentContext instances."""

    def test_different_ids_different_instances(self) -> None:
        ctx_a = get_context("session-A")
        ctx_b = get_context("session-B")
        assert ctx_a is not ctx_b

    def test_same_id_same_instance(self) -> None:
        ctx1 = get_context("session-C")
        ctx2 = get_context("session-C")
        assert ctx1 is ctx2

    def test_isolated_counters(self) -> None:
        update_context("iso-1", prompt_tokens=500, completion_tokens=200)
        update_context("iso-2", prompt_tokens=100, completion_tokens=50)

        ctx1 = get_context("iso-1")
        ctx2 = get_context("iso-2")

        assert ctx1.cumulative_prompt_tokens == 500
        assert ctx2.cumulative_prompt_tokens == 100
        assert ctx1.turn_count == 1
        assert ctx2.turn_count == 1


class TestRemoveContext:
    """remove_context deletes a session from storage."""

    def test_remove_existing(self) -> None:
        get_context("to-remove")
        remove_context("to-remove")
        # After removal, get_context creates a fresh one
        ctx = get_context("to-remove")
        assert ctx.cumulative_prompt_tokens == 0
        assert ctx.turn_count == 0

    def test_remove_nonexistent_no_error(self) -> None:
        # Must not raise
        remove_context("never-existed")


# === Extra safety tests ===


class TestZeroTokensTurns:
    """update_from_usage with zero tokens still increments turn_count."""

    def test_zero_tokens_increments_turn(self) -> None:
        ctx = AgentContext()
        ctx.update_from_usage(prompt_tokens=0, completion_tokens=0)
        assert ctx.turn_count == 1
        assert ctx.cumulative_prompt_tokens == 0
        assert ctx.last_context_size == 0
