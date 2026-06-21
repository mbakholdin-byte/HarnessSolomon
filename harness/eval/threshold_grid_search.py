"""Phase 7.5 — Threshold Grid Search for Tier Router Calibration.

Performs a full grid search over threshold parameters for the
heuristic tier router, evaluating each grid point against the
golden routing dataset to find the optimal thresholds that
maximize accuracy while minimizing cost.

**Trust boundary:** stdlib + ``csv`` + ``logging`` + ``math`` +
``itertools``. Imports ``RoutingEvent`` from
:mod:`harness.eval.calibration_parser`. NO imports from
``harness.agents``, ``harness.server``, or ``harness.context``.
"""

from __future__ import annotations

import csv
import itertools
import logging
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from harness.eval.calibration_parser import RoutingEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: USD per single LLM call per tier (flat — no token math in grid search).
COST_MODEL: dict[str, float] = {
    "t1": 0.001,
    "t2": 0.005,
    "t3": 0.020,
}

#: Base complexity keywords shared by all keyword lists.
BASE_KEYWORDS: list[str] = [
    "reasoning",
    "analyze",
    "prove",
    "derive",
    "evaluate",
]

#: Grid ranges for each threshold parameter.
GRID_RANGES: dict[str, list[float] | list[int] | list[list[str]]] = {
    "confidence_high": [0.6, 0.7, 0.75, 0.8, 0.85, 0.9],
    "confidence_low": [0.3, 0.4, 0.5, 0.55, 0.6],
    "t1_max_prompt_chars": [200, 350, 500, 750, 1000],
    "t1_max_context_tokens": [2000, 3000, 4000, 6000, 8000],
    "t3_min_prompt_chars": [3000, 4000, 5000, 7000, 10000],
    "t3_min_context_tokens": [16000, 24000, 32000, 48000, 64000],
    "complexity_keywords": [
        ["reasoning", "analyze", "prove", "derive", "evaluate"],
        [
            "reasoning",
            "analyze",
            "prove",
            "derive",
            "evaluate",
            "compare",
            "synthesize",
            "design",
            "compute",
        ],
    ],
}

#: Maximum grid combinations allowed before subsampling warning.
MAX_GRID_COMBINATIONS: int = 50_000

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GridPoint:
    """A single point in the threshold grid search space.

    Each combination of threshold values forms a candidate
    configuration for the heuristic tier router.
    """

    confidence_high: float
    confidence_low: float
    t1_max_prompt_chars: int
    t1_max_context_tokens: int
    t3_min_prompt_chars: int
    t3_min_context_tokens: int
    complexity_keywords: list[str]


@dataclass
class CalibrationResult:
    """Evaluation metrics for a single grid point against the golden dataset.

    Attributes:
        grid_point: The threshold configuration under test.
        accuracy: Fraction of correct tier predictions (status=ok only).
        total_cost_usd: Sum of simulated routing costs.
        t1_fraction: Fraction of events routed to T1.
        t3_fraction: Fraction of events routed to T3.
        fallback_rate: Fraction where predicted_tier != chosen_tier (all events).
        composite_score: accuracy - lambda * normalized_cost.
    """

    grid_point: GridPoint
    accuracy: float
    total_cost_usd: float
    t1_fraction: float
    t3_fraction: float
    fallback_rate: float
    composite_score: float = 0.0


# ---------------------------------------------------------------------------
# Grid builder
# ---------------------------------------------------------------------------


def build_grid() -> list[GridPoint]:
    """Generate all grid combinations from :data:`GRID_RANGES`.

    Uses :func:`itertools.product` to produce the Cartesian product
    of all threshold ranges. Complexity keyword lists are kept as-is.

    Returns:
        List of :class:`GridPoint` objects, one per combination.
        If total combinations exceed :data:`MAX_GRID_COMBINATIONS`,
        a warning is logged and random subsampling is applied.

    """
    ranges = GRID_RANGES
    raw_combinations = list(
        itertools.product(
            ranges["confidence_high"],
            ranges["confidence_low"],
            ranges["t1_max_prompt_chars"],
            ranges["t1_max_context_tokens"],
            ranges["t3_min_prompt_chars"],
            ranges["t3_min_context_tokens"],
            ranges["complexity_keywords"],
        )
    )

    total = len(raw_combinations)
    logger.info("grid search: %d total combinations", total)

    if total > MAX_GRID_COMBINATIONS:
        logger.warning(
            "grid combinations (%d) exceed max (%d) — "
            "subsampling to %d via random seed 42",
            total,
            MAX_GRID_COMBINATIONS,
            MAX_GRID_COMBINATIONS,
        )
        random.seed(42)
        raw_combinations = random.sample(raw_combinations, MAX_GRID_COMBINATIONS)

    grid: list[GridPoint] = []
    for conf_high, conf_low, t1_pc, t1_ct, t3_pc, t3_ct, kw_list in raw_combinations:
        # Ensure kw_list is a list of strings (it may be a tuple from product)
        keywords: list[str] = (
            list(kw_list) if isinstance(kw_list, (list, tuple)) else [str(kw_list)]
        )
        grid.append(
            GridPoint(
                confidence_high=float(conf_high),
                confidence_low=float(conf_low),
                t1_max_prompt_chars=int(t1_pc),
                t1_max_context_tokens=int(t1_ct),
                t3_min_prompt_chars=int(t3_pc),
                t3_min_context_tokens=int(t3_ct),
                complexity_keywords=keywords,
            )
        )

    logger.info("grid built: %d points ready for evaluation", len(grid))
    return grid


# ---------------------------------------------------------------------------
# Tier simulation
# ---------------------------------------------------------------------------


def simulate_tier(event: RoutingEvent, grid: GridPoint) -> str:
    """Predict tier for a single routing event using grid thresholds.

    Pure function — no LLM calls, no I/O.

    Logic (applied in order, first match wins):

        1. If ``prompt_tokens > grid.t3_min_context_tokens`` OR
           ``prompt_len_chars > grid.t3_min_prompt_chars`` → ``"t3"``
        2. If ``event.has_complexity_keyword`` is True → ``"t3"``
        3. If ``prompt_tokens < grid.t1_max_context_tokens`` AND
           ``prompt_len_chars < grid.t1_max_prompt_chars`` → ``"t1"``
        4. Else → ``"t2"`` (fallback)

    Args:
        event: A single routing event from the golden dataset.
        grid: Threshold configuration to test.

    Returns:
        One of ``"t1"``, ``"t2"``, or ``"t3"``.

    """
    # Rule 1: large prompt/context → force T3
    if (
        event.prompt_tokens > grid.t3_min_context_tokens
        or event.prompt_len_chars > grid.t3_min_prompt_chars
    ):
        return "t3"

    # Rule 2: complexity keyword → T3
    if event.has_complexity_keyword:
        return "t3"

    # Rule 3: small prompt/context → T1
    if (
        event.prompt_tokens < grid.t1_max_context_tokens
        and event.prompt_len_chars < grid.t1_max_prompt_chars
    ):
        return "t1"

    # Rule 4: everything else → T2
    return "t2"


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_grid(
    events: list[RoutingEvent],
    grid: GridPoint,
) -> CalibrationResult:
    """Evaluate a single grid point against the golden dataset.

    For each event, simulates the predicted tier via
    :func:`simulate_tier` and compares it to the golden
    ``chosen_tier``.

    Args:
        events: Golden routing dataset (737+ rows).
        grid: Threshold configuration to evaluate.

    Returns:
        :class:`CalibrationResult` with accuracy, cost, fractions,
        and fallback rate. ``composite_score`` is set to 0.0 (filled
        in later by :func:`run_grid_search`).

    """
    if not events:
        return CalibrationResult(
            grid_point=grid,
            accuracy=0.0,
            total_cost_usd=0.0,
            t1_fraction=0.0,
            t3_fraction=0.0,
            fallback_rate=0.0,
        )

    total = len(events)
    total_cost = 0.0
    correct_ok = 0
    ok_count = 0
    t1_count = 0
    t3_count = 0
    mismatches = 0

    for event in events:
        predicted = simulate_tier(event, grid)
        cost = COST_MODEL.get(predicted, 0.0)
        total_cost += cost

        if predicted == "t1":
            t1_count += 1
        elif predicted == "t3":
            t3_count += 1

        # Accuracy: only status=ok events
        if event.status == "ok":
            ok_count += 1
            if predicted == event.chosen_tier.lower():
                correct_ok += 1

        # Fallback: predicted != chosen (all events)
        if predicted != event.chosen_tier.lower():
            mismatches += 1

    accuracy = correct_ok / ok_count if ok_count > 0 else 0.0
    fallback_rate = mismatches / total

    return CalibrationResult(
        grid_point=grid,
        accuracy=round(accuracy, 6),
        total_cost_usd=round(total_cost, 6),
        t1_fraction=round(t1_count / total, 6),
        t3_fraction=round(t3_count / total, 6),
        fallback_rate=round(fallback_rate, 6),
    )


# ---------------------------------------------------------------------------
# Grid search runner
# ---------------------------------------------------------------------------


def run_grid_search(
    events: list[RoutingEvent],
    lambda_cost: float = 0.5,
    top_n: int = 10,
) -> list[CalibrationResult]:
    """Run full grid search and return top-N results sorted by composite score.

    Composite score formula::

        score = accuracy - lambda * normalized_cost

    where ``normalized_cost`` is the per-point total cost mapped to
    ``[0, 1]`` using the min/max of all grid points.

    Args:
        events: Golden routing dataset.
        lambda_cost: Weight of cost penalty in composite score
            (higher = stricter cost preference).
        top_n: Number of top results to return.

    Returns:
        Top-N :class:`CalibrationResult` sorted by composite score
        (descending). If ``events`` is empty, returns an empty list.

    """
    if not events:
        logger.warning("run_grid_search: no events provided, returning empty list")
        return []

    grid = build_grid()
    logger.info(
        "evaluating %d grid points against %d events (lambda=%.2f)",
        len(grid),
        len(events),
        lambda_cost,
    )

    results: list[CalibrationResult] = []
    for gp in grid:
        result = evaluate_grid(events, gp)
        results.append(result)

    # Normalize costs to [0, 1] across all results
    costs = [r.total_cost_usd for r in results]
    min_cost = min(costs)
    max_cost = max(costs)
    cost_range = max_cost - min_cost + 1e-9

    for r in results:
        normalized_cost = (r.total_cost_usd - min_cost) / cost_range
        r.composite_score = round(r.accuracy - lambda_cost * normalized_cost, 6)

    # Sort by composite score descending, then by accuracy descending,
    # then by cost ascending as tiebreakers
    results.sort(
        key=lambda r: (r.composite_score, r.accuracy, -r.total_cost_usd),
        reverse=True,
    )

    top_results = results[:top_n]
    logger.info(
        "grid search complete: top-%d results (best composite=%.6f, "
        "best accuracy=%.4f, best cost=$%.6f)",
        len(top_results),
        top_results[0].composite_score if top_results else 0.0,
        top_results[0].accuracy if top_results else 0.0,
        top_results[0].total_cost_usd if top_results else 0.0,
    )

    return top_results


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------


#: CSV column order for calibration results.
RESULT_CSV_COLUMNS: list[str] = [
    "confidence_high",
    "confidence_low",
    "t1_max_prompt_chars",
    "t1_max_context_tokens",
    "t3_min_prompt_chars",
    "t3_min_context_tokens",
    "complexity_keywords",
    "accuracy",
    "total_cost_usd",
    "t1_fraction",
    "t3_fraction",
    "fallback_rate",
    "composite_score",
]


def write_results(
    results: list[CalibrationResult],
    output_path: Path,
) -> None:
    """Write calibration results to CSV.

    Creates parent directories if needed. Overwrites existing files.

    Args:
        results: Calibration results from :func:`run_grid_search`.
        output_path: Output CSV path (e.g.
            ``data/calibration/threshold_grid_results.csv``).

    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESULT_CSV_COLUMNS)
        writer.writeheader()
        for r in results:
            gp = r.grid_point
            writer.writerow(
                {
                    "confidence_high": gp.confidence_high,
                    "confidence_low": gp.confidence_low,
                    "t1_max_prompt_chars": gp.t1_max_prompt_chars,
                    "t1_max_context_tokens": gp.t1_max_context_tokens,
                    "t3_min_prompt_chars": gp.t3_min_prompt_chars,
                    "t3_min_context_tokens": gp.t3_min_context_tokens,
                    "complexity_keywords": ";".join(gp.complexity_keywords),
                    "accuracy": r.accuracy,
                    "total_cost_usd": r.total_cost_usd,
                    "t1_fraction": r.t1_fraction,
                    "t3_fraction": r.t3_fraction,
                    "fallback_rate": r.fallback_rate,
                    "composite_score": r.composite_score,
                }
            )

    logger.info(
        "calibration results written: %d rows → %s",
        len(results),
        output_path,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "GridPoint",
    "CalibrationResult",
    "COST_MODEL",
    "BASE_KEYWORDS",
    "GRID_RANGES",
    "build_grid",
    "simulate_tier",
    "evaluate_grid",
    "run_grid_search",
    "write_results",
]
