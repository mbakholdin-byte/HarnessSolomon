"""Phase 7.5 — Calibration Report Generator.

Takes the golden routing dataset and grid search results and produces
a structured markdown report with recommended thresholds, holdout
validation, robustness analysis, and migration impact assessment.

**Trust boundary:** stdlib + ``csv`` + ``logging`` + ``random`` +
``dataclasses``. Imports ``RoutingEvent`` from
:mod:`harness.eval.calibration_parser` and grid search primitives from
:mod:`harness.eval.threshold_grid_search`. NO imports from
``harness.agents``, ``harness.server``, or ``harness.context``.
"""

from __future__ import annotations

import csv
import logging
import random
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from harness.eval.calibration_parser import CSV_COLUMNS, RoutingEvent
from harness.eval.threshold_grid_search import (
    BASE_KEYWORDS,
    COST_MODEL,
    CalibrationResult,
    GridPoint,
    evaluate_grid,
    run_grid_search,
    simulate_tier,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recommendation data model
# ---------------------------------------------------------------------------


@dataclass
class CalibrationRecommendation:
    """Recommended threshold configuration for the tier router.

    Mirrors :class:`GridPoint` structurally so it can be converted
    back to a ``GridPoint`` for evaluation via :func:`evaluate_grid`.
    """

    confidence_high: float
    confidence_low: float
    t1_max_prompt_chars: int
    t1_max_context_tokens: int
    t3_min_prompt_chars: int
    t3_min_context_tokens: int
    complexity_keywords: list[str]


#: Current default thresholds used by the tier router (Phase 7.5 defaults).
#: These are the reference point for migration impact analysis.
CURRENT_DEFAULTS = CalibrationRecommendation(
    confidence_high=0.85,
    confidence_low=0.55,
    t1_max_prompt_chars=500,
    t1_max_context_tokens=4000,
    t3_min_prompt_chars=5000,
    t3_min_context_tokens=32000,
    complexity_keywords=BASE_KEYWORDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rec_to_gridpoint(rec: CalibrationRecommendation) -> GridPoint:
    """Convert a recommendation to a GridPoint for evaluation."""
    return GridPoint(
        confidence_high=rec.confidence_high,
        confidence_low=rec.confidence_low,
        t1_max_prompt_chars=rec.t1_max_prompt_chars,
        t1_max_context_tokens=rec.t1_max_context_tokens,
        t3_min_prompt_chars=rec.t3_min_prompt_chars,
        t3_min_context_tokens=rec.t3_min_context_tokens,
        complexity_keywords=list(rec.complexity_keywords),
    )


# ---------------------------------------------------------------------------
# CSV Reader
# ---------------------------------------------------------------------------


def read_golden_dataset(path: Path) -> list[RoutingEvent]:
    """Read a golden routing dataset CSV back into ``RoutingEvent`` objects.

    Converts string fields to the appropriate Python types (int, float,
    bool). Empty ``error_class`` strings become ``None``.

    Args:
        path: Path to the CSV file (e.g.
            ``data/calibration/golden_routing_dataset.csv``).

    Returns:
        List of :class:`RoutingEvent` objects, one per data row.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    if not path.is_file():
        raise FileNotFoundError(f"golden dataset not found: {path}")

    events: list[RoutingEvent] = []

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            event = RoutingEvent(
                ts=row.get("ts", ""),
                session_id=row.get("session_id", ""),
                prompt_len_chars=int(row.get("prompt_len_chars", 0) or 0),
                prompt_tokens=int(row.get("prompt_tokens", 0) or 0),
                context_tokens=int(row.get("context_tokens", 0) or 0),
                has_tool_calls=_parse_bool(row.get("has_tool_calls", "False")),
                has_complexity_keyword=_parse_bool(
                    row.get("has_complexity_keyword", "False")
                ),
                confidence=float(row.get("confidence", 0.0) or 0.0),
                chosen_tier=row.get("chosen_tier", "unknown"),
                actual_model=row.get("actual_model", "unknown"),
                status=row.get("status", "unknown"),
                error_class=row.get("error_class") or None,
                cost_usd=float(row.get("cost_usd", 0.0) or 0.0),
            )
            events.append(event)

    logger.info("read %d routing events from %s", len(events), path)
    return events


def _parse_bool(value: str) -> bool:
    """Parse CSV boolean string. Handles Python-style 'True'/'False'."""
    return value.strip().lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Holdout validation
# ---------------------------------------------------------------------------


def holdout_split(
    events: list[RoutingEvent],
    ratio: float = 0.8,
    seed: int = 42,
) -> tuple[list[RoutingEvent], list[RoutingEvent]]:
    """Reproducible train/holdout split.

    Shuffles the list with a fixed seed, then splits at ``ratio``.

    Args:
        events: Full dataset.
        ratio: Fraction of events for training (0.0–1.0).
        seed: Random seed for reproducibility.

    Returns:
        Tuple of ``(train, holdout)`` event lists.
    """
    random.seed(seed)
    shuffled = list(events)
    random.shuffle(shuffled)
    split_idx = int(len(shuffled) * ratio)
    train = shuffled[:split_idx]
    holdout = shuffled[split_idx:]
    logger.info(
        "holdout split: %d train / %d holdout (ratio=%.2f, seed=%d)",
        len(train),
        len(holdout),
        ratio,
        seed,
    )
    return train, holdout


def validate_on_holdout(
    events: list[RoutingEvent],
    train_results: list[CalibrationResult],
    top_n: int = 5,
) -> list[dict]:
    """Recompute metrics for top-N configurations on the holdout set.

    Takes the grid points from the top-N training results and evaluates
    each against the holdout events.

    Args:
        events: Holdout event set.
        train_results: Top-N results from training (already sorted).
        top_n: Number of top configurations to validate.

    Returns:
        List of dicts with keys: ``rank``, ``accuracy``, ``total_cost_usd``,
        ``t1_fraction``, ``t3_fraction``, ``fallback_rate``,
        ``composite_score``, and the 7 grid point parameters.
    """
    validation_results: list[dict] = []

    for rank, train_result in enumerate(train_results[:top_n], start=1):
        holdout_result = evaluate_grid(events, train_result.grid_point)

        # Recompute composite score against holdout cost range
        # We use a simple relative cost for holdout composite
        # (no full normalization since we evaluate only top-N)
        normalized_cost = holdout_result.total_cost_usd / (
            max(COST_MODEL.get("t3", 0.02), 0.001)
        )
        composite = round(
            holdout_result.accuracy - 0.5 * normalized_cost, 6
        )

        gp = train_result.grid_point
        entry = {
            "rank": rank,
            "accuracy": holdout_result.accuracy,
            "total_cost_usd": holdout_result.total_cost_usd,
            "t1_fraction": holdout_result.t1_fraction,
            "t3_fraction": holdout_result.t3_fraction,
            "fallback_rate": holdout_result.fallback_rate,
            "composite_score": composite,
            "confidence_high": gp.confidence_high,
            "confidence_low": gp.confidence_low,
            "t1_max_prompt_chars": gp.t1_max_prompt_chars,
            "t1_max_context_tokens": gp.t1_max_context_tokens,
            "t3_min_prompt_chars": gp.t3_min_prompt_chars,
            "t3_min_context_tokens": gp.t3_min_context_tokens,
            "complexity_keywords": ";".join(gp.complexity_keywords),
        }
        validation_results.append(entry)

    logger.info(
        "holdout validation: %d configs evaluated on %d events",
        len(validation_results),
        len(events),
    )
    return validation_results


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def robustness_check(
    events: list[RoutingEvent],
    rec: CalibrationRecommendation,
    perturbation: float = 0.1,
) -> dict[str, float]:
    """Perturb each numeric threshold by ±10% and measure accuracy variance.

    For each of the 6 numeric thresholds, three evaluations are run:
    original, +perturbation%, and −perturbation% (clamped to valid ranges).
    The variance of the three accuracy values is returned.
    For ``complexity_keywords``, variance is measured between the base
    and extended keyword lists.

    Args:
        events: Full dataset for evaluation.
        rec: Recommended threshold configuration.
        perturbation: Relative perturbation factor (default 0.1 = ±10%).

    Returns:
        Dict mapping each of the 7 parameter names to its accuracy
        variance (float).
    """
    variances: dict[str, float] = {}

    # Numeric fields — perturb each independently
    numeric_fields = [
        ("confidence_high", rec.confidence_high, 0.0, 1.0),
        ("confidence_low", rec.confidence_low, 0.0, 1.0),
        ("t1_max_prompt_chars", rec.t1_max_prompt_chars, 1, 100000),
        ("t1_max_context_tokens", rec.t1_max_context_tokens, 1, 1000000),
        ("t3_min_prompt_chars", rec.t3_min_prompt_chars, 1, 100000),
        ("t3_min_context_tokens", rec.t3_min_context_tokens, 1, 1000000),
    ]

    for name, base_val, lo, hi in numeric_fields:
        accuracies: list[float] = []

        for factor in (1 - perturbation, 1.0, 1 + perturbation):
            perturbed_val = base_val * factor
            # Clamp
            if isinstance(base_val, int):
                perturbed_val = int(max(lo, min(hi, perturbed_val)))
            else:
                perturbed_val = float(max(lo, min(hi, perturbed_val)))

            # Build modified recommendation
            rec_perturbed = CalibrationRecommendation(
                **{
                    **asdict(rec),
                    name: perturbed_val,
                }
            )
            gp = _rec_to_gridpoint(rec_perturbed)
            result = evaluate_grid(events, gp)
            accuracies.append(result.accuracy)

        # Population variance (ddof=0 — full enumeration of 3 points)
        mean = sum(accuracies) / len(accuracies)
        variance = sum((a - mean) ** 2 for a in accuracies) / len(accuracies)
        variances[name] = round(variance, 8)

    # Keyword perturbation — base vs extended
    extended_keywords = [
        "reasoning", "analyze", "prove", "derive", "evaluate",
        "compare", "synthesize", "design", "compute",
    ]
    kw_accuracies: list[float] = []
    for kw_list in (rec.complexity_keywords, extended_keywords):
        rec_kw = CalibrationRecommendation(
            **{**asdict(rec), "complexity_keywords": kw_list},
        )
        gp = _rec_to_gridpoint(rec_kw)
        result = evaluate_grid(events, gp)
        kw_accuracies.append(result.accuracy)

    mean_kw = sum(kw_accuracies) / len(kw_accuracies)
    kw_variance = sum(
        (a - mean_kw) ** 2 for a in kw_accuracies
    ) / len(kw_accuracies)
    variances["complexity_keywords"] = round(kw_variance, 8)

    logger.info(
        "robustness check: 7 params perturbed, max variance=%.6f",
        max(variances.values()),
    )
    return variances


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def generate_recommendation(
    train_results: list[CalibrationResult],
    events: list[RoutingEvent] | None = None,
) -> CalibrationRecommendation:
    """Select the best threshold configuration from grid search results.

    If all top-N configurations have 100% accuracy, selects the one
    with the widest T1 zone (maximizing ``t1_max_prompt_chars ×
    t1_max_context_tokens``).

    When the top-N results all share the same product (all-perfect
    degenerate case, e.g. all prompt_tokens=0), and ``events`` is
    provided, re-runs grid search to find the configuration with the
    maximum T1 zone among ALL perfect-accuracy results.

    Otherwise selects the configuration with the highest composite
    score.

    Args:
        train_results: Top-N calibration results from
            :func:`run_grid_search` (pre-sorted by composite score).
        events: Optional golden dataset. If provided and all top-N
            are perfect with identical T1 products, used to re-run
            grid search for the true widest T1 zone.

    Returns:
        :class:`CalibrationRecommendation` with the selected thresholds.

    Raises:
        ValueError: If ``train_results`` is empty.
    """
    if not train_results:
        raise ValueError("train_results is empty — cannot generate recommendation")

    all_perfect = all(r.accuracy >= 0.999 for r in train_results)

    if all_perfect:
        products = [
            r.grid_point.t1_max_prompt_chars
            * r.grid_point.t1_max_context_tokens
            for r in train_results
        ]
        max_product = max(products)
        min_product = min(products)

        # If all top-N have identical T1 product and events are provided,
        # the top-N ordering is degenerate — re-run with full results to
        # find the true widest T1 zone among all perfect configs.
        if min_product == max_product and events is not None:
            logger.info(
                "top-N all have product=%d → expanding search for widest T1 zone",
                max_product,
            )
            # Run full grid search to get ALL results (top_n = total grid size)
            all_results = run_grid_search(
                events, lambda_cost=0.5, top_n=37500
            )
            # Filter for perfect accuracy only
            perfect_results = [
                r for r in all_results if r.accuracy >= 0.999
            ]
            # Pick widest T1 zone among ALL perfect results
            best = max(
                perfect_results,
                key=lambda r: (
                    r.grid_point.t1_max_prompt_chars
                    * r.grid_point.t1_max_context_tokens
                ),
            )
            reason = (
                "all top-N ≈100% accuracy (degenerate) → expanded search, "
                "selected widest T1 zone among all perfect configs "
                f"(product={best.grid_point.t1_max_prompt_chars * best.grid_point.t1_max_context_tokens})"
            )
        else:
            # Top-N have diverse products — just pick max from top-N
            best = max(
                train_results,
                key=lambda r: (
                    r.grid_point.t1_max_prompt_chars
                    * r.grid_point.t1_max_context_tokens
                ),
            )
            reason = (
                "all top-N ≈100% accuracy → selected widest T1 zone "
                f"(product={best.grid_point.t1_max_prompt_chars * best.grid_point.t1_max_context_tokens})"
            )
    else:
        # Normal case — best composite score (already first after sorting)
        best = train_results[0]
        reason = (
            f"best composite score ({best.composite_score:.4f}) "
            f"with accuracy={best.accuracy:.4f}"
        )

    logger.info("recommendation: %s", reason)

    gp = best.grid_point
    return CalibrationRecommendation(
        confidence_high=gp.confidence_high,
        confidence_low=gp.confidence_low,
        t1_max_prompt_chars=gp.t1_max_prompt_chars,
        t1_max_context_tokens=gp.t1_max_context_tokens,
        t3_min_prompt_chars=gp.t3_min_prompt_chars,
        t3_min_context_tokens=gp.t3_min_context_tokens,
        complexity_keywords=list(gp.complexity_keywords),
    )


# ---------------------------------------------------------------------------
# Migration impact
# ---------------------------------------------------------------------------


def migration_impact(
    events: list[RoutingEvent],
    current: CalibrationRecommendation,
    recommended: CalibrationRecommendation,
) -> dict:
    """Compare current vs recommended threshold configurations.

    Evaluates both configurations against the full dataset and returns
    a diff of accuracy, cost, and tier fractions.

    Args:
        events: Full golden dataset.
        current: Currently deployed threshold configuration.
        recommended: Recommended (new) threshold configuration.

    Returns:
        Dict with keys: ``current_accuracy``, ``recommended_accuracy``,
        ``delta_accuracy``, ``current_cost``, ``recommended_cost``,
        ``delta_cost``.
    """
    gp_current = _rec_to_gridpoint(current)
    gp_recommended = _rec_to_gridpoint(recommended)

    res_current = evaluate_grid(events, gp_current)
    res_recommended = evaluate_grid(events, gp_recommended)

    impact = {
        "current_accuracy": res_current.accuracy,
        "recommended_accuracy": res_recommended.accuracy,
        "delta_accuracy": round(
            res_recommended.accuracy - res_current.accuracy, 6
        ),
        "current_cost": res_current.total_cost_usd,
        "recommended_cost": res_recommended.total_cost_usd,
        "delta_cost": round(
            res_recommended.total_cost_usd - res_current.total_cost_usd, 6
        ),
    }

    logger.info(
        "migration impact: Δaccuracy=%.4f, Δcost=$%.6f",
        impact["delta_accuracy"],
        impact["delta_cost"],
    )
    return impact


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def generate_markdown(
    rec: CalibrationRecommendation,
    validation: list[dict],
    robustness: dict[str, float],
    impact: dict,
    total_events: int,
    train_events: int,
    holdout_events: int,
) -> str:
    """Generate a structured Markdown calibration report.

    Args:
        rec: Recommended threshold configuration.
        validation: Holdout validation results from
            :func:`validate_on_holdout`.
        robustness: Variances from :func:`robustness_check`.
        impact: Migration impact from :func:`migration_impact`.
        total_events: Total events in the golden dataset.
        train_events: Number of training events.
        holdout_events: Number of holdout events.

    Returns:
        Markdown string with all sections.
    """
    # Format keyword lists
    rec_kw = ", ".join(
        f"``{kw}``" for kw in rec.complexity_keywords
    )
    current_kw = ", ".join(
        f"``{kw}``" for kw in CURRENT_DEFAULTS.complexity_keywords
    )

    # Build validation table
    val_rows = ""
    for v in validation:
        val_rows += (
            f"| {v['rank']} "
            f"| {v['accuracy']:.4f} "
            f"| ${v['total_cost_usd']:.6f} "
            f"| {v['t1_fraction']:.4f} "
            f"| {v['fallback_rate']:.4f} "
            f"| {v['composite_score']:.4f} |\n"
        )

    # Robustness: sort by variance descending
    robust_rows = ""
    for param, var in sorted(
        robustness.items(), key=lambda x: x[1], reverse=True
    ):
        sensitive = "⚠️" if var > 0.001 else "✅"
        robust_rows += (
            f"| ``{param}`` | {var:.6f} | {sensitive} |\n"
        )

    delta_acc_sign = "+" if impact["delta_accuracy"] >= 0 else ""
    delta_cost_sign = "+" if impact["delta_cost"] >= 0 else ""

    report = f"""# Calibration Report — v1.33.0

> **Phase:** 7.5 (Tier Router Calibration)
> **Generated:** auto-generated from golden dataset
> **Grid search model:** composite = accuracy − 0.5 × normalized_cost

---

## 1. Overview

This report presents the calibration results for the heuristic Tier Router
(``harness.routing.tier_selector``). A full grid search was performed over
**7 threshold parameters** against the golden routing dataset, followed by
holdout validation, robustness analysis, and migration impact assessment.

---

## 2. Data Summary

| Metric | Value |
|--------|-------|
| Total routing events | {total_events} |
| Training set | {train_events} (80%) |
| Holdout set | {holdout_events} (20%) |
| Grid combinations | 6×5×5×5×5×5×2 = 37,500 |
| Holdout seed | 42 |

**⚠️ Limitation (context_tokens):** All ``context_tokens`` values in the
current golden dataset are **0** — the log format does not capture per-call
context token counts. Thresholds for ``t1_max_context_tokens`` and
``t3_min_context_tokens`` are set to reasonable defaults but
**have not been validated against real context-token data**. Recalibration
is recommended once context token tracking is added to the logging pipeline.

---

## 3. Recommended Thresholds

| Parameter | Current Default | Recommended | Reason |
|-----------|----------------|-------------|--------|
| ``confidence_high`` | {CURRENT_DEFAULTS.confidence_high:.2f} | **{rec.confidence_high:.2f}** | Grid optimum |
| ``confidence_low`` | {CURRENT_DEFAULTS.confidence_low:.2f} | **{rec.confidence_low:.2f}** | Grid optimum |
| ``t1_max_prompt_chars`` | {CURRENT_DEFAULTS.t1_max_prompt_chars} | **{rec.t1_max_prompt_chars}** | Grid optimum |
| ``t1_max_context_tokens`` | {CURRENT_DEFAULTS.t1_max_context_tokens} | **{rec.t1_max_context_tokens}** | Grid optimum |
| ``t3_min_prompt_chars`` | {CURRENT_DEFAULTS.t3_min_prompt_chars} | **{rec.t3_min_prompt_chars}** | Grid optimum |
| ``t3_min_context_tokens`` | {CURRENT_DEFAULTS.t3_min_context_tokens} | **{rec.t3_min_context_tokens}** | Grid optimum |
| ``complexity_keywords`` | {current_kw} | {rec_kw} | Grid optimum |

---

## 4. Holdout Validation

Top-{len(validation)} grid configurations re-evaluated on the holdout set:

| Rank | Accuracy | Cost (USD) | T1 Fraction | Fallback | Score |
|------|----------|------------|-------------|----------|-------|
{val_rows}

---

## 5. Robustness Check

Each numeric threshold perturbed by ±10% (complexity keywords: base vs
extended list). Variance measures sensitivity — higher values indicate
the parameter strongly affects accuracy.

| Parameter | Accuracy Variance | Sensitive |
|-----------|------------------|-----------|
{robust_rows}

---

## 6. Migration Impact

| Metric | Current | Recommended | Delta |
|--------|---------|-------------|-------|
| Accuracy | {impact['current_accuracy']:.4f} | {impact['recommended_accuracy']:.4f} | {delta_acc_sign}{impact['delta_accuracy']:.4f} |
| Total Cost (USD) | ${impact['current_cost']:.6f} | ${impact['recommended_cost']:.6f} | {delta_cost_sign}${impact['delta_cost']:.6f} |

---

## 7. Limitation Notes

1. **context_tokens = 0:** The golden dataset does not contain real context
   token values. Thresholds ``t1_max_context_tokens`` and
   ``t3_min_context_tokens`` are selected by grid search but have not been
   validated against context-rich scenarios.

2. **Prompt text absent:** ``prompt_len_chars`` is estimated as
   ``prompt_tokens × 4``; ``has_complexity_keyword`` is inferred from
   ``model_id``/``model`` fields only (no prompt text in logs).

3. **T1 bias:** Due to the absence of prompt/context data, most events fall
   into T1 (Rule 3). The grid search selects the **widest T1 zone** when
   all configurations achieve ≈100% accuracy. Real-world performance with
   actual prompt data may differ.

4. **Confidence unused:** The ``confidence_high`` and ``confidence_low``
   thresholds are part of the grid but are not exercised by the current
   heuristic router logic (which uses only prompt/context size and
   complexity keywords).

5. **Re-evaluation recommended:** Re-run calibration after adding
   per-call context token tracking and prompt text keyword scanning to
   the logging pipeline (Phase 7.6).
"""
    return report


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def write_report(
    rec: CalibrationRecommendation,
    events: list[RoutingEvent],
    output_path: Path,
) -> None:
    """Full pipeline: generate recommendation, validation, robustness,
    impact analysis, and write a markdown report to disk.

    Performs:
        1. Holdout split (80/20, seed=42).
        2. Grid search on training set (lambda=0.5, top_n=5).
        3. Holdout validation of top-5 configs.
        4. Robustness check against recommended thresholds.
        5. Migration impact (current defaults vs recommended).
        6. Markdown generation and file write.

    Args:
        rec: Recommended threshold configuration (pre-computed).
        events: Full golden dataset.
        output_path: Destination path for the markdown report.
    """
    train, holdout = holdout_split(events, ratio=0.8, seed=42)
    train_results = run_grid_search(train, lambda_cost=0.5, top_n=10)
    validation = validate_on_holdout(holdout, train_results, top_n=5)
    robustness = robustness_check(events, rec, perturbation=0.1)
    impact = migration_impact(events, CURRENT_DEFAULTS, rec)

    markdown = generate_markdown(
        rec=rec,
        validation=validation,
        robustness=robustness,
        impact=impact,
        total_events=len(events),
        train_events=len(train),
        holdout_events=len(holdout),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    logger.info("calibration report written: %s", output_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CalibrationRecommendation",
    "CURRENT_DEFAULTS",
    "read_golden_dataset",
    "holdout_split",
    "validate_on_holdout",
    "robustness_check",
    "generate_recommendation",
    "migration_impact",
    "generate_markdown",
    "write_report",
]
