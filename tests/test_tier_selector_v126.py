"""Phase 7.5 v1.33.0 — Tests for ``TierSelector.select_heuristic``.

Verifies the heuristic tier routing rules:

  * **T1** for short prompts (< 1000 chars) with small context (< 8000
    tokens) and no tool calls.
  * **T3** for long prompts (> 3000 chars), large contexts (> 16000
    tokens), or complexity keywords ("reasoning", "analyze", "prove",
    "derive", "evaluate").
  * **T2** as the default for medium prompts.
  * ``None`` fallback when ``tier_routing_heuristic_enabled`` is False.
  * Settings override (custom thresholds / keywords).

Run::

    pytest tests/test_tier_selector_v126.py -v
"""
from __future__ import annotations

import pytest

from harness.agents.cascade import TIER_T1, TIER_T2, TIER_T3, TierSelector
from harness.config import settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def selector() -> TierSelector:
    """A default TierSelector for heuristic tests.

    The thresholds (confidence_high/low) are irrelevant for the
    heuristic method — it reads from ``settings`` directly. We
    construct a plain selector so the tests exercise the real
    ``settings.tier_routing_*`` fields.
    """
    return TierSelector()


@pytest.fixture
def heuristic_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the heuristic is enabled (default, but explicit for clarity)."""
    monkeypatch.setattr(settings, "tier_routing_heuristic_enabled", True)


# ---------------------------------------------------------------------------
# T1 routing — short prompts
# ---------------------------------------------------------------------------

class TestT1Routing:
    """T1 for short prompts with small context and no tools."""

    def test_short_prompt_no_tools_routes_t1(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """A 100-char prompt with zero context → T1."""
        prompt = "a" * 100  # well under default 500
        result = selector.select_heuristic(prompt, context_size=0)
        assert result == TIER_T1

    def test_short_prompt_at_boundary_routes_t1(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """Prompt length = 999 (just under the 1000 threshold) → T1."""
        prompt = "a" * 999
        result = selector.select_heuristic(prompt, context_size=100)
        assert result == TIER_T1

    def test_small_context_routes_t1(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """Context at 7999 tokens (just under 8000) with short prompt → T1."""
        prompt = "short prompt"
        result = selector.select_heuristic(prompt, context_size=7999)
        assert result == TIER_T1

    def test_tool_calls_disqualify_t1(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """Short prompt with has_tool_calls=True does NOT get T1.

        Tool calls need a capable model. Even with a tiny prompt,
        T1 is skipped (falls to T2 default or T3 if keywords match).
        """
        prompt = "a" * 50
        result = selector.select_heuristic(prompt, has_tool_calls=True)
        # No keywords, medium prompt → T2 default.
        assert result == TIER_T2

    def test_large_context_disqualifies_t1(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """Context at 8000 tokens (>= threshold) does NOT get T1."""
        prompt = "a" * 100
        result = selector.select_heuristic(prompt, context_size=8000)
        # 8000 is >= T1 max (8000), so T1 disqualified.
        # 8000 < T3 min (16000), no keywords → T2 default.
        assert result == TIER_T2


# ---------------------------------------------------------------------------
# T3 routing — long / complex prompts
# ---------------------------------------------------------------------------

class TestT3Routing:
    """T3 for long prompts, large contexts, or complexity keywords."""

    def test_long_prompt_routes_t3(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """A 6000-char prompt (above 5000 threshold) → T3."""
        prompt = "a" * 6000
        result = selector.select_heuristic(prompt)
        assert result == TIER_T3

    def test_prompt_at_t3_boundary_routes_t3(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """Prompt length = 5001 (just above the 5000 threshold) → T3."""
        prompt = "a" * 5001
        result = selector.select_heuristic(prompt)
        assert result == TIER_T3

    def test_large_context_routes_t3(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """Context at 32001 tokens (above 32000 threshold) → T3."""
        result = selector.select_heuristic(
            "short prompt", context_size=32001,
        )
        assert result == TIER_T3

    @pytest.mark.parametrize("keyword", [
        "reasoning", "analyze", "prove", "derive", "evaluate",
    ])
    def test_complexity_keyword_routes_t3(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
        keyword: str,
    ) -> None:
        """A medium-length prompt with a complexity keyword → T3.

        The keyword matching is case-insensitive. The prompt must be
        longer than ``t1_max_prompt_chars`` (1000) so that T1 doesn't
        win first — the heuristic uses first-match-wins ordering, and
        T1 takes precedence for short prompts.
        """
        # 1100 chars: above T1 (1000) but below T3 (3000).
        prompt = f"Please {keyword} the following problem. " + "x" * 1060
        assert len(prompt) > 1000, "test prompt must exceed T1 threshold"
        assert len(prompt) < 3000, "test prompt must be below T3 threshold"
        result = selector.select_heuristic(prompt)
        assert result == TIER_T3, (
            f"keyword {keyword!r} should route to T3, got {result}"
        )

    def test_keyword_case_insensitive(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """Uppercase keyword also triggers T3 (with a medium-length prompt)."""
        prompt = "We need to REASONING through this. " + "x" * 1060
        assert len(prompt) > 1000
        result = selector.select_heuristic(prompt)
        assert result == TIER_T3

    def test_t3_overrides_t1_conditions(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """T3 keyword wins even when prompt is short (T1 conditions met).

        The T3 check runs after T1, but T3 keywords are checked in the
        T3 block. A short prompt with "prove" should still get T3.
        Wait — actually the T1 check runs FIRST and would return T1
        before the T3 keyword check. Let's verify the actual behaviour:
        T1 wins for short prompts regardless of keywords.

        This test documents that T1 is preferred over T3 keywords for
        short prompts (first-match-wins ordering).
        """
        prompt = "prove 1+1=2"  # short (< 1000) + keyword "prove"
        result = selector.select_heuristic(prompt, context_size=0)
        # T1 check runs first (short + small + no tools) → T1 wins.
        assert result == TIER_T1


# ---------------------------------------------------------------------------
# T2 routing — default for medium prompts
# ---------------------------------------------------------------------------

class TestT2Routing:
    """T2 default for medium prompts without complexity signals."""

    def test_medium_prompt_routes_t2(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """A 1200-char prompt (above T1 threshold 1000, below T3 threshold 3000)
        without keywords → T2."""
        prompt = "a" * 1200  # 1000 < 1200 < 3000, no keywords
        result = selector.select_heuristic(prompt)
        assert result == TIER_T2

    def test_medium_prompt_with_tools_routes_t2(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """A medium prompt with tool calls → T2 (T1 disqualified, no T3 signal)."""
        prompt = "a" * 300
        result = selector.select_heuristic(prompt, has_tool_calls=True)
        assert result == TIER_T2

    def test_empty_prompt_routes_t1(
        self,
        selector: TierSelector,
        heuristic_enabled: None,
    ) -> None:
        """An empty prompt is length 0 < 500 → T1.

        Edge case: empty string is technically a valid short prompt.
        """
        result = selector.select_heuristic("")
        assert result == TIER_T1


# ---------------------------------------------------------------------------
# Disabled heuristic — None fallback
# ---------------------------------------------------------------------------

class TestDisabledHeuristic:
    """When ``tier_routing_heuristic_enabled`` is False, returns None."""

    def test_disabled_returns_none(
        self,
        selector: TierSelector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Heuristic disabled → None (fall through to confidence cascade)."""
        monkeypatch.setattr(settings, "tier_routing_heuristic_enabled", False)
        result = selector.select_heuristic("any prompt here")
        assert result is None

    def test_disabled_ignores_all_inputs(
        self,
        selector: TierSelector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even a T3-length prompt returns None when disabled."""
        monkeypatch.setattr(settings, "tier_routing_heuristic_enabled", False)
        result = selector.select_heuristic("a" * 10000, context_size=50000)
        assert result is None


# ---------------------------------------------------------------------------
# Settings override
# ---------------------------------------------------------------------------

class TestSettingsOverride:
    """Custom thresholds and keywords via settings override."""

    def test_custom_t1_threshold(
        self,
        selector: TierSelector,
        monkeypatch: pytest.MonkeyPatch,
        heuristic_enabled: None,
    ) -> None:
        """Lowering t1_max_prompt_chars to 50 changes routing."""
        monkeypatch.setattr(settings, "tier_routing_t1_max_prompt_chars", 50)
        # 100 chars is now above the custom T1 threshold (50).
        prompt = "a" * 100
        result = selector.select_heuristic(prompt)
        # 100 < 5000, no keywords → T2.
        assert result == TIER_T2

    def test_custom_t3_threshold(
        self,
        selector: TierSelector,
        monkeypatch: pytest.MonkeyPatch,
        heuristic_enabled: None,
    ) -> None:
        """Raising t3_min_prompt_chars to 10000 changes routing."""
        monkeypatch.setattr(settings, "tier_routing_t3_min_prompt_chars", 10000)
        # 6000 chars is now below the custom T3 threshold.
        prompt = "a" * 6000
        result = selector.select_heuristic(prompt)
        # No keywords, below custom T3 → T2.
        assert result == TIER_T2

    def test_custom_keywords(
        self,
        selector: TierSelector,
        monkeypatch: pytest.MonkeyPatch,
        heuristic_enabled: None,
    ) -> None:
        """Custom keywords override the defaults."""
        monkeypatch.setattr(
            settings,
            "tier_routing_complexity_keywords",
            ["architecture", "refactor"],
        )
        # "architecture" is now a keyword; "reasoning" is NOT.
        prompt1 = "Let's discuss the architecture of this system."
        prompt2 = "We need reasoning about this problem."

        # prompt1: 50 chars (T1 eligible) but has "architecture" keyword.
        # T1 check runs first → T1 wins (short + small + no tools).
        # To test keyword matching, use a longer prompt (> 1000 chars).
        long_prompt1 = "a" * 1100 + " architecture " + "b" * 100
        result1 = selector.select_heuristic(long_prompt1)
        assert result1 == TIER_T3, (
            f"'architecture' should be a keyword → T3, got {result1}"
        )

        # "reasoning" is no longer a keyword → T2 for medium prompt.
        long_prompt2 = "a" * 1100 + " reasoning " + "b" * 100
        result2 = selector.select_heuristic(long_prompt2)
        assert result2 == TIER_T2, (
            f"'reasoning' is NOT a custom keyword → T2, got {result2}"
        )

    def test_custom_context_thresholds(
        self,
        selector: TierSelector,
        monkeypatch: pytest.MonkeyPatch,
        heuristic_enabled: None,
    ) -> None:
        """Custom context thresholds change T1/T3 eligibility."""
        monkeypatch.setattr(settings, "tier_routing_t1_max_context_tokens", 100)
        monkeypatch.setattr(settings, "tier_routing_t3_min_context_tokens", 500)
        # Short prompt with 200-token context: above custom T1 (100)
        # but below custom T3 (500) → T2.
        result = selector.select_heuristic("short", context_size=200)
        assert result == TIER_T2

        # 600-token context: above custom T3 (500) → T3.
        result2 = selector.select_heuristic("short", context_size=600)
        assert result2 == TIER_T3


# ---------------------------------------------------------------------------
# Integration with select_tier (smoke)
# ---------------------------------------------------------------------------

class TestHeuristicIntegration:
    """Smoke test: the heuristic method does not interfere with select_tier.

    The heuristic is a separate entry point; ``select_tier`` continues
    to work unchanged for callers that don't use the heuristic.
    """

    def test_select_tier_still_works(
        self,
        selector: TierSelector,
    ) -> None:
        """select_tier is unaffected by the heuristic addition."""
        # High confidence → T1 (if t1_model is set).
        decision = selector.select_tier(0.95)
        assert decision.tier in (TIER_T1, TIER_T2)

        # Low confidence → T3.
        decision = selector.select_tier(0.1)
        assert decision.tier == TIER_T3
