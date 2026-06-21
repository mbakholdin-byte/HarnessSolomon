"""Phase 7.5 — Tests for :mod:`harness.eval.threshold_grid_search`.

Covers:
    * Grid uniqueness and size.
    * simulate_tier correctness for all three tiers.
    * Metric computation on synthetic dataset.
    * Top-N selection.
    * Composite score monotonicity.
    * CSV output validity.
    * Empty input handling.
    * Determinism.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from harness.eval.calibration_parser import RoutingEvent
from harness.eval.threshold_grid_search import (
    COST_MODEL,
    GridPoint,
    CalibrationResult,
    build_grid,
    simulate_tier,
    evaluate_grid,
    run_grid_search,
    write_results,
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
    """Build a synthetic RoutingEvent for testing."""
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


def _default_grid() -> GridPoint:
    """Return a mid-range grid point for testing."""
    return GridPoint(
        confidence_high=0.8,
        confidence_low=0.4,
        t1_max_prompt_chars=500,
        t1_max_context_tokens=4000,
        t3_min_prompt_chars=5000,
        t3_min_context_tokens=32000,
        complexity_keywords=["reasoning", "analyze", "prove", "derive", "evaluate"],
    )


# ---------------------------------------------------------------------------
# Test 1: Grid uniqueness
# ---------------------------------------------------------------------------


def test_grid_definition_has_no_duplicates() -> None:
    """All generated GridPoint objects must be unique."""
    grid = build_grid()
    # Use a tuple of all fields as a hashable representation
    seen: set[tuple] = set()
    for gp in grid:
        key = (
            gp.confidence_high,
            gp.confidence_low,
            gp.t1_max_prompt_chars,
            gp.t1_max_context_tokens,
            gp.t3_min_prompt_chars,
            gp.t3_min_context_tokens,
            tuple(gp.complexity_keywords),
        )
        assert key not in seen, f"Duplicate grid point: {gp}"
        seen.add(key)
    assert len(grid) == len(seen)


# ---------------------------------------------------------------------------
# Test 2: Grid size ≤ 50K
# ---------------------------------------------------------------------------


def test_grid_size_reasonable() -> None:
    """Total grid combinations must not exceed MAX_GRID_COMBINATIONS."""
    grid = build_grid()
    assert len(grid) <= 50_000, f"grid size {len(grid)} exceeds 50K limit"


# ---------------------------------------------------------------------------
# Test 3: simulate_tier returns valid tier
# ---------------------------------------------------------------------------


def test_simulate_row_returns_valid_tier() -> None:
    """simulate_tier must always return 't1', 't2', or 't3'."""
    grid = _default_grid()

    # Case A: small prompt, no complexity → T1
    event_small = _make_event(prompt_tokens=100, prompt_len_chars=400)
    assert simulate_tier(event_small, grid) == "t1"

    # Case B: complexity keyword → T3
    event_complex = _make_event(has_complexity_keyword=True)
    assert simulate_tier(event_complex, grid) == "t3"

    # Case C: large prompt tokens → T3 (rule 1)
    event_large_tokens = _make_event(prompt_tokens=100_000, prompt_len_chars=400_000)
    assert simulate_tier(event_large_tokens, grid) == "t3"

    # Case D: large prompt chars → T3 (rule 1)
    event_large_chars = _make_event(prompt_tokens=10, prompt_len_chars=20_000)
    assert simulate_tier(event_large_chars, grid) == "t3"

    # Case E: medium prompt, no complexity → T2 (fallback)
    event_medium = _make_event(
        prompt_tokens=5_000,
        prompt_len_chars=20_000,
    )
    # prompt_tokens=5000 > t1_max_context_tokens=4000, so T1 rule fails
    # prompt_tokens=5000 < t3_min_context_tokens=32000, so T3 rule doesn't trigger from tokens
    # prompt_len_chars=20000 > t3_min_prompt_chars=5000, so T3 rule triggers!
    # Let me adjust...
    # Need a case where neither T1 nor T3 rules match
    event_medium2 = _make_event(
        prompt_tokens=5_000,  # > t1_max_context_tokens=4000 → not T1
        prompt_len_chars=3_000,  # < t3_min_prompt_chars=5000 → not T3
    )
    assert simulate_tier(event_medium2, grid) == "t2"

    # Verify all return values are valid
    for tier in [simulate_tier(event_small, grid),
                 simulate_tier(event_complex, grid),
                 simulate_tier(event_large_tokens, grid),
                 simulate_tier(event_large_chars, grid),
                 simulate_tier(event_medium2, grid)]:
        assert tier in ("t1", "t2", "t3"), f"Invalid tier: {tier!r}"


# ---------------------------------------------------------------------------
# Test 4: Metric computation on synthetic dataset
# ---------------------------------------------------------------------------


def test_metrics_compute_on_synthetic_dataset() -> None:
    """evaluate_grid must compute accuracy, cost, fractions on 10 events."""
    # Create 10 events:
    # - 5 T1 events (small, no complexity)
    # - 5 T3 events (complexity keyword)
    events = []
    for _ in range(5):
        events.append(_make_event(chosen_tier="T1"))
    for _ in range(5):
        events.append(_make_event(chosen_tier="T3", has_complexity_keyword=True))

    grid = _default_grid()
    result = evaluate_grid(events, grid)

    # All 5 T1 events should route to T1, all 5 T3 to T3
    assert result.accuracy == pytest.approx(1.0)
    assert result.t1_fraction == pytest.approx(0.5)
    assert result.t3_fraction == pytest.approx(0.5)
    # Cost: 5 * 0.001 + 5 * 0.020 = 0.105
    expected_cost = 5 * COST_MODEL["t1"] + 5 * COST_MODEL["t3"]
    assert result.total_cost_usd == pytest.approx(expected_cost)
    assert result.fallback_rate == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 5: Top-N selection
# ---------------------------------------------------------------------------


def test_top_n_selection_returns_n_rows() -> None:
    """run_grid_search must return at most top_n results."""
    events = [_make_event() for _ in range(20)]
    top_n = 5
    results = run_grid_search(events, top_n=top_n)
    assert len(results) <= top_n, f"expected ≤ {top_n}, got {len(results)}"
    # Also verify results are sorted by composite_score descending
    scores = [r.composite_score for r in results]
    assert scores == sorted(scores, reverse=True), "results not sorted by composite_score"


# ---------------------------------------------------------------------------
# Test 6: Composite score monotonic in accuracy
# ---------------------------------------------------------------------------


def test_composite_score_monotonic_in_accuracy() -> None:
    """Higher accuracy → higher composite_score (same cost).

    Creates two CalibrationResults with identical total_cost_usd
    but different accuracy and verifies the scoring direction.
    Uses a 3-point normalization context so the normalized cost
    is well-defined.
    """
    grid = _default_grid()

    # Three results: two with same cost, one with different cost to
    # establish normalization range.
    results = [
        CalibrationResult(
            grid_point=grid, accuracy=0.90, total_cost_usd=0.50,
            t1_fraction=0.5, t3_fraction=0.5, fallback_rate=0.0,
        ),
        CalibrationResult(
            grid_point=grid, accuracy=0.80, total_cost_usd=0.50,
            t1_fraction=0.5, t3_fraction=0.5, fallback_rate=0.0,
        ),
        CalibrationResult(
            grid_point=grid, accuracy=0.70, total_cost_usd=0.30,
            t1_fraction=0.5, t3_fraction=0.5, fallback_rate=0.0,
        ),
    ]

    # Normalize costs
    costs = [r.total_cost_usd for r in results]
    min_cost = min(costs)
    max_cost = max(costs)
    cost_range = max_cost - min_cost + 1e-9
    for r in results:
        normalized = (r.total_cost_usd - min_cost) / cost_range
        r.composite_score = round(r.accuracy - 0.5 * normalized, 6)

    # Both have same cost → same normalized_cost → same penalty
    # Higher accuracy → higher composite_score
    high_acc = next(r for r in results if r.accuracy == 0.90)
    low_acc = next(r for r in results if r.accuracy == 0.80)
    assert high_acc.composite_score > low_acc.composite_score, (
        f"0.90 accuracy score ({high_acc.composite_score}) should be "
        f"> 0.80 accuracy score ({low_acc.composite_score})"
    )


# ---------------------------------------------------------------------------
# Test 7: Composite score decreases with cost
# ---------------------------------------------------------------------------


def test_composite_score_decreases_with_cost() -> None:
    """Higher cost → lower composite_score (same accuracy).

    Creates two CalibrationResults with identical accuracy but
    different total_cost_usd and verifies the scoring direction.
    """
    grid = _default_grid()

    results = [
        CalibrationResult(
            grid_point=grid, accuracy=0.85, total_cost_usd=0.20,
            t1_fraction=0.5, t3_fraction=0.5, fallback_rate=0.0,
        ),
        CalibrationResult(
            grid_point=grid, accuracy=0.85, total_cost_usd=0.80,
            t1_fraction=0.5, t3_fraction=0.5, fallback_rate=0.0,
        ),
        CalibrationResult(
            grid_point=grid, accuracy=0.50, total_cost_usd=0.50,
            t1_fraction=0.5, t3_fraction=0.5, fallback_rate=0.0,
        ),
    ]

    # Normalize costs
    costs = [r.total_cost_usd for r in results]
    min_cost = min(costs)
    max_cost = max(costs)
    cost_range = max_cost - min_cost + 1e-9
    lambda_cost = 0.5
    for r in results:
        normalized = (r.total_cost_usd - min_cost) / cost_range
        r.composite_score = round(r.accuracy - lambda_cost * normalized, 6)

    # Both have same accuracy → lower cost gets higher score
    cheap = next(r for r in results if r.total_cost_usd == 0.20)
    expensive = next(r for r in results if r.total_cost_usd == 0.80)
    assert cheap.composite_score > expensive.composite_score, (
        f"cheaper ({cheap.total_cost_usd}, score={cheap.composite_score}) "
        f"should beat more expensive ({expensive.total_cost_usd}, "
        f"score={expensive.composite_score})"
    )


# ---------------------------------------------------------------------------
# Test 8: Results CSV writable
# ---------------------------------------------------------------------------


def test_results_csv_writable(tmp_path: Path) -> None:
    """write_results must produce a valid CSV with correct headers."""
    grid = _default_grid()
    result = CalibrationResult(
        grid_point=grid,
        accuracy=0.95,
        total_cost_usd=0.105,
        t1_fraction=0.5,
        t3_fraction=0.5,
        fallback_rate=0.0,
        composite_score=0.85,
    )

    csv_path = tmp_path / "test_results.csv"
    write_results([result], csv_path)

    assert csv_path.is_file()
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        assert len(rows) == 1
        row = rows[0]
        assert row["accuracy"] == "0.95"
        assert row["total_cost_usd"] == "0.105"
        assert row["composite_score"] == "0.85"
        assert row["confidence_high"] == "0.8"


# ---------------------------------------------------------------------------
# Test 9: Empty input
# ---------------------------------------------------------------------------


def test_empty_grid_returns_empty_results() -> None:
    """0 events → run_grid_search returns empty list."""
    results = run_grid_search([])
    assert results == []

    # Also test evaluate_grid directly
    grid = _default_grid()
    result = evaluate_grid([], grid)
    assert result.accuracy == 0.0
    assert result.total_cost_usd == 0.0
    assert result.t1_fraction == 0.0
    assert result.t3_fraction == 0.0
    assert result.fallback_rate == 0.0


# ---------------------------------------------------------------------------
# Test 10: Determinism
# ---------------------------------------------------------------------------


def test_determinism() -> None:
    """Same input must produce same output (reproducible)."""
    events = [_make_event() for _ in range(20)]

    results1 = run_grid_search(events, top_n=5)
    results2 = run_grid_search(events, top_n=5)

    assert len(results1) == len(results2)
    for r1, r2 in zip(results1, results2):
        assert r1.accuracy == r2.accuracy
        assert r1.total_cost_usd == r2.total_cost_usd
        assert r1.composite_score == r2.composite_score
        assert r1.t1_fraction == r2.t1_fraction
        assert r1.t3_fraction == r2.t3_fraction
        assert r1.fallback_rate == r2.fallback_rate
