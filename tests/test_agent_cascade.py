"""Tests for harness.agents.cascade (Phase 2.1, Step 1).

Covers:
  - Threshold boundaries (0.85 → T1, 0.849 → T2, 0.55 → T2, 0.549 → T3)
  - Fallback=True forces T3 (router said "I don't know")
  - T1 disabled (empty t1_model) degrades to T2
  - CascadeDecision immutability (frozen)
  - TierSelector rejects low >= high at construction
  - Confidence clamping (out-of-range values)
  - AgentRunner.run(model_override=...) reaches the AgentLoop
  - AgentRunner.run without override keeps spec.model (regression)
  - AgentRunner.stream(model_override=...) reaches AgentLoop
  - module-level select_tier() functional form
  - RouterDecision.tier field (Phase 2.1 observability)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harness.agents.cascade import (
    CascadeDecision,
    T1_DISABLED,
    TIER_T1,
    TIER_T2,
    TIER_T3,
    TierSelector,
    select_tier,
)
from harness.agents.runner import AgentRunner
from harness.agents.spec import AgentSpec
from harness.server.llm.router import CompletionResult, LLMRouter, StreamEvent


# === TierSelector unit tests (pure-function logic) ===

class TestTierSelectorBoundaries:
    """The three threshold rules."""

    def test_high_confidence_returns_t1(self) -> None:
        sel = TierSelector(
            t1_model="qwen3:8b", t2_model="glm-4.7", t3_model="MiniMax-M2.7",
            confidence_high=0.85, confidence_low=0.55,
        )
        d = sel.select_tier(0.95)
        assert d.tier == TIER_T1
        assert d.chosen_model == "qwen3:8b"

    def test_boundary_at_high_threshold_returns_t1(self) -> None:
        """0.85 is the inclusive T1 boundary (>=, not >)."""
        sel = TierSelector(
            t1_model="qwen3:8b", confidence_high=0.85, confidence_low=0.55,
        )
        assert sel.select_tier(0.85).tier == TIER_T1

    def test_just_below_high_threshold_returns_t2(self) -> None:
        """0.849 is in the T2 band [low, high)."""
        sel = TierSelector(
            t1_model="qwen3:8b", t2_model="glm-4.7",
            confidence_high=0.85, confidence_low=0.55,
        )
        d = sel.select_tier(0.849)
        assert d.tier == TIER_T2
        assert d.chosen_model == "glm-4.7"

    def test_boundary_at_low_threshold_returns_t2(self) -> None:
        """0.55 is the inclusive T2 boundary (>=, not >)."""
        sel = TierSelector(
            t2_model="glm-4.7",
            confidence_high=0.85, confidence_low=0.55,
        )
        assert sel.select_tier(0.55).tier == TIER_T2

    def test_just_below_low_threshold_returns_t3(self) -> None:
        """0.549 is below the T2 floor — T3."""
        sel = TierSelector(
            t2_model="glm-4.7", t3_model="MiniMax-M2.7",
            confidence_high=0.85, confidence_low=0.55,
        )
        d = sel.select_tier(0.549)
        assert d.tier == TIER_T3
        assert d.chosen_model == "MiniMax-M2.7"

    def test_zero_confidence_returns_t3(self) -> None:
        sel = TierSelector(t3_model="MiniMax-M2.7")
        assert sel.select_tier(0.0).tier == TIER_T3


class TestTierSelectorFallback:
    """When the router gave up, we don't take the cheap-local risk."""

    def test_fallback_true_forces_t3(self) -> None:
        sel = TierSelector(
            t1_model="qwen3:8b", t2_model="glm-4.7", t3_model="MiniMax-M2.7",
        )
        # Even at 0.99 confidence, fallback wins.
        d = sel.select_tier(0.99, fallback=True)
        assert d.tier == TIER_T3
        assert d.chosen_model == "MiniMax-M2.7"
        assert "fallback" in d.reason.lower()

    def test_fallback_false_uses_normal_rules(self) -> None:
        sel = TierSelector(t1_model="qwen3:8b")
        assert sel.select_tier(0.99, fallback=False).tier == TIER_T1


class TestTierSelectorT1Disabled:
    """When T1 model is empty (e.g. CI without Ollama), T1 is skipped."""

    def test_empty_t1_model_degrades_to_t2(self) -> None:
        sel = TierSelector(
            t1_model="", t2_model="glm-4.7", t3_model="MiniMax-M2.7",
            confidence_high=0.85, confidence_low=0.55,
        )
        d = sel.select_tier(0.99)
        assert d.tier == TIER_T2
        assert "T1 disabled" in d.reason

    def test_t1_disabled_constant(self) -> None:
        """The T1_DISABLED sentinel is the empty string — operators
        can use either ``""`` or the constant in their config."""
        assert T1_DISABLED == ""


class TestCascadeDecisionImmutability:
    """CascadeDecision is frozen: the selector cannot be bypassed
    by mutating the result downstream."""

    def test_frozen(self) -> None:
        d = CascadeDecision(chosen_model="qwen3:8b", tier=TIER_T1, reason="x")
        with pytest.raises(Exception):  # ValidationError on frozen
            d.tier = TIER_T2  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(Exception):
            CascadeDecision(
                chosen_model="qwen3:8b", tier=TIER_T1,
                reason="x", smuggled="nope",  # type: ignore[call-arg]
            )

    def test_tier_pattern_validation(self) -> None:
        """Only T1/T2/T3 are valid tier names."""
        with pytest.raises(Exception):
            CascadeDecision(chosen_model="x", tier="T4", reason="y")


class TestTierSelectorConstruction:
    """Validation at construction time, not on the first call."""

    def test_low_geq_high_raises(self) -> None:
        with pytest.raises(ValueError, match="confidence_low"):
            TierSelector(confidence_low=0.9, confidence_high=0.5)

    def test_low_equal_high_raises(self) -> None:
        """Boundary: equal thresholds mean no T2 band exists."""
        with pytest.raises(ValueError, match="confidence_low"):
            TierSelector(confidence_low=0.7, confidence_high=0.7)

    def test_defaults_from_settings(self) -> None:
        """Without explicit thresholds, the selector pulls from
        ``harness.config.settings``."""
        sel = TierSelector()
        # Defaults: high=0.60, low=0.30, t1=qwen3:8b (Phase 7.5 calibrated).
        assert sel.confidence_high == 0.60
        assert sel.confidence_low == 0.30
        assert sel.t1_model == "qwen3:8b"


class TestConfidenceClamping:
    """Chatty models sometimes report > 1.0; we clamp defensively."""

    def test_above_one_clamped_to_one(self) -> None:
        """1.001 is treated as 1.0 — T1."""
        sel = TierSelector(t1_model="qwen3:8b")
        assert sel.select_tier(1.001).tier == TIER_T1

    def test_negative_clamped_to_zero(self) -> None:
        """-0.5 is treated as 0.0 — T3."""
        sel = TierSelector(t3_model="MiniMax-M2.7")
        assert sel.select_tier(-0.5).tier == TIER_T3


class TestModuleLevelSelectTier:
    """The functional form is a thin wrapper around the class."""

    def test_functional_form_matches_class(self) -> None:
        d1 = select_tier(0.95)
        d2 = TierSelector().select_tier(0.95)
        assert d1.tier == d2.tier
        assert d1.chosen_model == d2.chosen_model

    def test_functional_form_with_fallback(self) -> None:
        d = select_tier(0.99, fallback=True)
        assert d.tier == TIER_T3


# === Runner integration tests (model_override reaches AgentLoop) ===

class _ScriptedRouter:
    """Minimal scripted LLMRouter — just records the model arg
    and yields a single ``done`` event so AgentLoop terminates.

    Mirrors the pattern in ``test_agent_runner.py`` but minimal:
    we don't need a full FakeRouter for the cascade test, we
    just need to verify which model id reaches AgentLoop.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def streaming_completion(self, *, model: str, messages, **kwargs):
        self.calls.append({"model": model, "method": "streaming", "n_messages": len(messages)})
        yield StreamEvent(type="done", content="", cost=0.0, usage={})

    async def completion(self, *, model: str, messages, **kwargs) -> CompletionResult:
        self.calls.append({"model": model, "method": "completion", "n_messages": len(messages)})
        # Empty content + no tool_calls = AgentLoop terminates on next
        # iteration (Phase 0 behaviour: empty assistant_message => done).
        return CompletionResult(content="", tool_calls=None, usage={}, cost=0.0)


def _spec(*, model: str = "MiniMax-M2.7", worktree_required: bool = False) -> AgentSpec:
    """Helper: a minimal AgentSpec that doesn't need a worktree."""
    return AgentSpec(
        name="test",
        model=model,
        tools=["read_file"],
        permissions="read-only",
        system_prompt="You are a test agent.",
        max_iterations=2,
        worktree_required=worktree_required,
    )


@pytest.mark.asyncio
async def test_runner_uses_spec_model_by_default(tmp_path: Path) -> None:
    """No model_override → AgentLoop receives spec.model."""
    router = _ScriptedRouter()
    runner = AgentRunner(router=router, repo=tmp_path)  # type: ignore[arg-type]
    spec = _spec(model="MiniMax-M2.7")
    result = await runner.run(spec, "hello", worktree_id=None)
    assert result.spec.model == "MiniMax-M2.7"
    # The router should have been called once with spec.model.
    assert router.calls
    assert router.calls[0]["model"] == "MiniMax-M2.7"


@pytest.mark.asyncio
async def test_runner_model_override_reaches_agent_loop(tmp_path: Path) -> None:
    """model_override="qwen3:8b" → AgentLoop receives qwen3:8b,
    even though spec.model is the cloud T3 default."""
    router = _ScriptedRouter()
    runner = AgentRunner(router=router, repo=tmp_path)  # type: ignore[arg-type]
    spec = _spec(model="MiniMax-M2.7")
    await runner.run(spec, "hello", worktree_id=None, model_override="qwen3:8b")
    assert router.calls
    assert router.calls[0]["model"] == "qwen3:8b"


@pytest.mark.asyncio
async def test_runner_empty_model_override_falls_back_to_spec(tmp_path: Path) -> None:
    """Empty string is treated as "no override" (falsy), so spec.model
    is used. This avoids accidental empty-arg bugs in CLI flags."""
    router = _ScriptedRouter()
    runner = AgentRunner(router=router, repo=tmp_path)  # type: ignore[arg-type]
    spec = _spec(model="glm-4.7")
    await runner.run(spec, "hello", worktree_id=None, model_override="")
    assert router.calls[0]["model"] == "glm-4.7"


@pytest.mark.asyncio
async def test_runner_cascade_integration(tmp_path: Path) -> None:
    """End-to-end: TierSelector picks T1, runner passes the chosen
    model to AgentLoop. This is the headline use case for cascade."""
    router = _ScriptedRouter()
    runner = AgentRunner(router=router, repo=tmp_path)  # type: ignore[arg-type]
    spec = _spec(model="MiniMax-M2.7")

    # Simulate a router that returned high confidence (0.92).
    decision = select_tier(0.92)
    assert decision.tier == TIER_T1  # sanity check
    await runner.run(
        spec, "hello", worktree_id=None,
        model_override=decision.chosen_model,
    )
    assert router.calls[0]["model"] == "qwen3:8b"


@pytest.mark.asyncio
async def test_runner_stream_model_override(tmp_path: Path) -> None:
    """Stream variant also honours model_override."""
    router = _ScriptedRouter()
    runner = AgentRunner(router=router, repo=tmp_path)  # type: ignore[arg-type]
    spec = _spec(model="MiniMax-M2.7")
    events: list[StreamEvent] = []
    async for e in runner.stream(spec, "hello", worktree_id=None, model_override="glm-4.7"):
        events.append(e)
    assert any(e.type == "done" for e in events)
    assert router.calls[0]["model"] == "glm-4.7"


def test_router_decision_tier_field_default_none() -> None:
    """RouterDecision.tier is optional — Phase 2.0 callers that
    construct it without cascade get tier=None."""
    from harness.agents.router import RouterDecision

    d = RouterDecision(agent="code", confidence=0.9, fallback=False)
    assert d.tier is None


def test_router_decision_tier_field_set() -> None:
    """When the cascade attaches a tier, the field carries it."""
    from harness.agents.router import RouterDecision

    d = RouterDecision(agent="code", confidence=0.9, fallback=False, tier=TIER_T1)
    assert d.tier == TIER_T1
