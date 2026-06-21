"""Phase 7.5 — Tests for :mod:`harness.eval.calibration_report`.

Covers:
    * Holdout split reproducibility.
    * Holdout validation metric computation.
    * Robustness check output structure.
    * CalibrationRecommendation field count.
    * Migration impact output structure.
    * Markdown report section completeness.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.eval.calibration_parser import RoutingEvent
from harness.eval.calibration_report import (
    CURRENT_DEFAULTS,
    CalibrationRecommendation,
    generate_markdown,
    generate_recommendation,
    holdout_split,
    migration_impact,
    robustness_check,
    validate_on_holdout,
)
from harness.eval.threshold_grid_search import (
    BASE_KEYWORDS,
    CalibrationResult,
    GridPoint,
    evaluate_grid,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    prompt_tokens: int = 0,
    prompt_len_chars: int = 0,
    context_tokens: int = 0,
    has_complexity_keyword: bool = False,
    has_tool_calls: bool = False,
    chosen_tier: str = "T1",
    status: str = "ok",
    confidence: float = 0.7,
    cost_usd: float = 0.0,
    ts: str = "1000000.0",
    session_id: str = "test",
    actual_model: str = "test-model",
    error_class: str | None = None,
) -> RoutingEvent:
    """Factory helper for synthetic RoutingEvent instances."""
    return RoutingEvent(
        ts=ts,
        session_id=session_id,
        prompt_len_chars=prompt_len_chars,
        prompt_tokens=prompt_tokens,
        context_tokens=context_tokens,
        has_tool_calls=has_tool_calls,
        has_complexity_keyword=has_complexity_keyword,
        confidence=confidence,
        chosen_tier=chosen_tier,
        actual_model=actual_model,
        status=status,
        error_class=error_class,
        cost_usd=cost_usd,
    )


def _make_synthetic_events(n: int = 100) -> list[RoutingEvent]:
    """Create a mix of T1 and T3 events for testing.

    T1: low prompt, no complexity keyword.
    T3: high prompt OR complexity keyword.
    Each event gets a unique ``ts`` for identification.
    """
    events: list[RoutingEvent] = []
    for i in range(n):
        if i < 70:
            # T1 events — small prompt
            events.append(
                _make_event(
                    ts=str(1000000.0 + i),
                    prompt_tokens=100,
                    prompt_len_chars=400,
                    has_complexity_keyword=False,
                    chosen_tier="T1",
                )
            )
        elif i < 90:
            # T3 events — large prompt
            events.append(
                _make_event(
                    ts=str(1000000.0 + i),
                    prompt_tokens=10000,
                    prompt_len_chars=40000,
                    has_complexity_keyword=False,
                    chosen_tier="T3",
                )
            )
        else:
            # T3 events — complexity keyword
            events.append(
                _make_event(
                    ts=str(1000000.0 + i),
                    prompt_tokens=100,
                    prompt_len_chars=400,
                    has_complexity_keyword=True,
                    chosen_tier="T3",
                )
            )
    return events


def _make_calibration_results(
    n: int = 5,
) -> list[CalibrationResult]:
    """Create synthetic CalibrationResult list with varied thresholds."""
    results: list[CalibrationResult] = []
    for i in range(n):
        gp = GridPoint(
            confidence_high=0.6 + i * 0.05,
            confidence_low=0.3 + i * 0.05,
            t1_max_prompt_chars=200 + i * 100,
            t1_max_context_tokens=2000 + i * 1000,
            t3_min_prompt_chars=3000 + i * 500,
            t3_min_context_tokens=16000 + i * 4000,
            complexity_keywords=list(BASE_KEYWORDS),
        )
        results.append(
            CalibrationResult(
                grid_point=gp,
                accuracy=round(0.8 + i * 0.04, 6),
                total_cost_usd=round(0.5 + i * 0.1, 6),
                t1_fraction=0.6,
                t3_fraction=0.2,
                fallback_rate=0.1,
                composite_score=round(0.7 + i * 0.05, 6),
            )
        )
    return results


# ---------------------------------------------------------------------------
# Test 1: Holdout split reproducibility
# ---------------------------------------------------------------------------


def test_holdout_split_reproducible() -> None:
    """Holdout split with seed=42 must produce the same result every time."""
    events = _make_synthetic_events(100)

    train1, holdout1 = holdout_split(events, ratio=0.8, seed=42)
    train2, holdout2 = holdout_split(events, ratio=0.8, seed=42)

    # Same length
    assert len(train1) == len(train2)
    assert len(holdout1) == len(holdout2)

    # Same content (order-sensitive — same shuffle)
    train1_ts = [e.ts for e in train1]
    train2_ts = [e.ts for e in train2]
    assert train1_ts == train2_ts

    # Ratio respected
    assert len(train1) == int(100 * 0.8)
    assert len(holdout1) == 100 - int(100 * 0.8)

    # Mutually exclusive
    train_set = {e.ts for e in train1}
    holdout_set = {e.ts for e in holdout1}
    assert train_set.isdisjoint(holdout_set)


# ---------------------------------------------------------------------------
# Test 2: Holdout validation recomputes metrics
# ---------------------------------------------------------------------------


def test_top5_validation_recomputes_metrics() -> None:
    """Holdout validation must return non-empty results with all metric keys."""
    events = _make_synthetic_events(100)
    results = _make_calibration_results(5)

    validation = validate_on_holdout(events, results, top_n=5)

    assert len(validation) == 5
    required_keys = {
        "rank", "accuracy", "total_cost_usd", "t1_fraction",
        "t3_fraction", "fallback_rate", "composite_score",
    }
    for entry in validation:
        assert required_keys.issubset(entry.keys())
        assert isinstance(entry["accuracy"], float)
        assert isinstance(entry["total_cost_usd"], float)
        assert 0.0 <= entry["accuracy"] <= 1.0


# ---------------------------------------------------------------------------
# Test 3: Robustness check returns variances
# ---------------------------------------------------------------------------


def test_robustness_check_returns_variance() -> None:
    """Robustness check must return 7 keys, all float."""
    events = _make_synthetic_events(100)
    rec = CalibrationRecommendation(
        confidence_high=0.85,
        confidence_low=0.55,
        t1_max_prompt_chars=500,
        t1_max_context_tokens=4000,
        t3_min_prompt_chars=5000,
        t3_min_context_tokens=32000,
        complexity_keywords=list(BASE_KEYWORDS),
    )

    variances = robustness_check(events, rec, perturbation=0.1)

    expected_keys = {
        "confidence_high",
        "confidence_low",
        "t1_max_prompt_chars",
        "t1_max_context_tokens",
        "t3_min_prompt_chars",
        "t3_min_context_tokens",
        "complexity_keywords",
    }
    assert set(variances.keys()) == expected_keys, (
        f"Expected 7 keys, got {len(variances)}: {variances.keys()}"
    )
    for key, value in variances.items():
        assert isinstance(value, float), (
            f"{key}={value!r} is not float, it is {type(value).__name__}"
        )
        assert value >= 0.0, f"{key} variance is negative: {value}"


# ---------------------------------------------------------------------------
# Test 4: Recommendation has 7 fields
# ---------------------------------------------------------------------------


def test_recommendation_has_7_fields() -> None:
    """CalibrationRecommendation must have exactly 7 dataclass fields."""
    import dataclasses

    fields_list = dataclasses.fields(CalibrationRecommendation)
    field_names = {f.name for f in fields_list}

    expected = {
        "confidence_high",
        "confidence_low",
        "t1_max_prompt_chars",
        "t1_max_context_tokens",
        "t3_min_prompt_chars",
        "t3_min_context_tokens",
        "complexity_keywords",
    }
    assert field_names == expected, (
        f"Expected 7 specific fields, got {len(fields_list)}: {field_names}"
    )


# ---------------------------------------------------------------------------
# Test 5: Migration impact computed
# ---------------------------------------------------------------------------


def test_migration_impact_computed_both_directions() -> None:
    """Migration impact dict must have 6 keys with proper types."""
    events = _make_synthetic_events(100)
    current = CURRENT_DEFAULTS
    recommended = CalibrationRecommendation(
        confidence_high=0.75,
        confidence_low=0.45,
        t1_max_prompt_chars=750,
        t1_max_context_tokens=6000,
        t3_min_prompt_chars=7000,
        t3_min_context_tokens=48000,
        complexity_keywords=list(BASE_KEYWORDS),
    )

    impact = migration_impact(events, current, recommended)

    expected_keys = {
        "current_accuracy",
        "recommended_accuracy",
        "delta_accuracy",
        "current_cost",
        "recommended_cost",
        "delta_cost",
    }
    assert set(impact.keys()) == expected_keys, (
        f"Expected 6 keys, got {len(impact)}: {impact.keys()}"
    )

    # All values are floats
    for key, value in impact.items():
        assert isinstance(value, float), (
            f"{key}={value!r} is not float"
        )

    # Delta is computed as recommended - current
    assert abs(
        impact["delta_accuracy"]
        - (impact["recommended_accuracy"] - impact["current_accuracy"])
    ) < 1e-9
    assert abs(
        impact["delta_cost"]
        - (impact["recommended_cost"] - impact["current_cost"])
    ) < 1e-9


# ---------------------------------------------------------------------------
# Test 6: Markdown report includes all sections
# ---------------------------------------------------------------------------


def test_report_markdown_includes_all_sections(tmp_path: Path) -> None:
    """Generated markdown must contain all 7 expected sections."""
    events = _make_synthetic_events(100)
    rec = CalibrationRecommendation(
        confidence_high=0.85,
        confidence_low=0.55,
        t1_max_prompt_chars=500,
        t1_max_context_tokens=4000,
        t3_min_prompt_chars=5000,
        t3_min_context_tokens=32000,
        complexity_keywords=list(BASE_KEYWORDS),
    )

    # Build minimal validation/robustness/impact data
    validation = [
        {
            "rank": 1,
            "accuracy": 0.95,
            "total_cost_usd": 0.5,
            "t1_fraction": 0.6,
            "t3_fraction": 0.2,
            "fallback_rate": 0.05,
            "composite_score": 0.9,
        }
    ]
    robustness = {
        "confidence_high": 0.0,
        "confidence_low": 0.001,
        "t1_max_prompt_chars": 0.0,
        "t1_max_context_tokens": 0.0,
        "t3_min_prompt_chars": 0.0,
        "t3_min_context_tokens": 0.002,
        "complexity_keywords": 0.0,
    }
    impact = {
        "current_accuracy": 0.95,
        "recommended_accuracy": 0.95,
        "delta_accuracy": 0.0,
        "current_cost": 0.5,
        "recommended_cost": 0.5,
        "delta_cost": 0.0,
    }

    markdown = generate_markdown(
        rec=rec,
        validation=validation,
        robustness=robustness,
        impact=impact,
        total_events=100,
        train_events=80,
        holdout_events=20,
    )

    # Write to file
    report_path = tmp_path / "test_report.md"
    report_path.write_text(markdown, encoding="utf-8")

    content = report_path.read_text(encoding="utf-8")

    section_titles = [
        "1. Overview",
        "2. Data Summary",
        "3. Recommended Thresholds",
        "4. Holdout Validation",
        "5. Robustness Check",
        "6. Migration Impact",
        "7. Limitation Notes",
    ]

    for title in section_titles:
        assert title in content, (
            f"Section '{title}' not found in report markdown"
        )
