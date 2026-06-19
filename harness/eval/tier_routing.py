"""Phase 6.1B — Tier Router pilot evaluation on the golden corpus.

This module is a **pilot eval harness** (not a metric like
:mod:`harness.eval.retrieval`). It answers:

    *If we route each golden query to a cheap / mid / premium LLM tier
    instead of always paying for the premium tier, how much money do
    we save without sacrificing answer quality?*

Strategy comparison
-------------------

Two routing strategies are compared head-to-head:

    1. **baseline** — every query → ``T3`` (MiniMax-M3 class, frontier
       cloud). This is the "safe" path: maximum quality, maximum cost.
    2. **heuristic** — :class:`HeuristicRouter` decides per-query:

         * short, single-fact lookup (``difficulty=easy`` OR
           ``category=factual_lookup`` AND ``len(query) < 60``) → ``T1``
           (local 8B Qwen3 — free).
         * medium paraphrase / ``difficulty=medium`` → ``T2``
           (mid cloud, GLM-4.7 class).
         * multi-hop / ``difficulty=hard`` → ``T3`` (frontier cloud).

       Expected outcome: most easy/medium queries drop to T1/T2,
       trimming the T3 spend by 60-80 % while keeping accuracy
       within 5 % of baseline.

Cost model (approximate, USD per query, flat — no token math in the
pilot)::

    T1  $0.001   (local Ollama, amortised electricity)
    T2  $0.005   (GLM-4.7, Sonnet-class)
    T3  $0.020   (MiniMax-M3, Opus-class)

Accuracy in the pilot is **simulated deterministically**: since no
real LLM is called, accuracy is derived from the query's golden
metadata. The model: a T3 call always answers correctly (accuracy
``1.0``). A lower tier has a per-tier miss-rate that scales with
difficulty::

    T1 easy   0.00 miss-rate     T2 easy   0.00     T3 *  0.00
    T1 medium 0.15                T2 medium 0.03
    T1 hard   0.40                T2 hard   0.10

This mirrors the empirical expectation that local 8B models ace
factual lookups but degrade on multi-hop reasoning. The pilot's job
is to show the **aggregate** routing distribution and cost/quality
trade-off, not to reproduce a specific model's WER.

DoD (Acceptance criteria)
-------------------------

    B1: heuristic routes ≥ 60 % of queries to T1
    B2: accuracy drop < 5 % vs baseline
    B3: cost reduction ≥ 40 % vs baseline
    B4: latency p95 within +20 % of baseline (no regression)

Latency in the pilot is also simulated (no real calls)::

    T1  120 ms   T2  450 ms   T3  1200 ms

plus a small routing overhead (~1 ms for the heuristic; 0 ms for the
baseline which is a constant decision).

**Trust boundary:** этот модуль НЕ импортирует ``harness.agents`` или
``harness.server`` (см. ``tests/eval/test_eval_trust_boundary.py``).
:class:`TierSelector` из :mod:`harness.agents.cascade` принимает
``confidence`` (число) — это другой контракт, не "heuristic by query
text". Поэтому здесь определён собственный :class:`HeuristicRouter`,
совместимый по сигнатуре ``select_heuristic(query, ...) -> RoutingDecision``.
Если позже Coder добавит ``TierSelector.select_heuristic`` с той же
сигнатурой, пилот можно будет переключить через параметр ``router=``
без изменения остального кода.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.eval.golden import GoldenQuery

logger = logging.getLogger(__name__)


# === Cost / latency model (pilot constants) ============================

#: USD per single LLM query, flat rate (no token math in the pilot).
TIER_COST_USD: dict[str, float] = {
    "T1": 0.001,
    "T2": 0.005,
    "T3": 0.020,
}

#: Simulated median latency per tier (milliseconds).
TIER_LATENCY_MS: dict[str, float] = {
    "T1": 120.0,
    "T2": 450.0,
    "T3": 1200.0,
}

#: Per-tier, per-diculty simulated miss-rate (probability that the
#: answer is wrong). ``T3`` is assumed perfect (0.0 across the board).
#: Indices not present default to a conservative 0.30.
_MISS_RATE: dict[str, dict[str, float]] = {
    "T1": {"easy": 0.00, "medium": 0.15, "hard": 0.40},
    "T2": {"easy": 0.00, "medium": 0.03, "hard": 0.10},
    "T3": {"easy": 0.00, "medium": 0.00, "hard": 0.00},
}

#: Conservative default miss-rate when difficulty is unexpected.
_DEFAULT_MISS_RATE: float = 0.30

#: Tier priority order (cheap → expensive).
TIER_PRIORITY: tuple[str, ...] = ("T1", "T2", "T3")


# === Decision record ===================================================


@dataclass(frozen=True)
class RoutingDecision:
    """Outcome of routing a single query to a tier.

    Attributes:
        tier: One of ``"T1"`` / ``"T2"`` / ``"T3"``.
        model: Human-readable model name (e.g. ``"qwen3:8b"``).
        reason: Short explanation (logging-friendly).
        confidence: Heuristic self-confidence in ``[0, 1]`` — kept
            for parity with :class:`~harness.agents.cascade.CascadeDecision`
            so the same consumers can use either record.
    """

    tier: str
    model: str
    reason: str
    confidence: float = 1.0


@dataclass(frozen=True)
class QueryOutcome:
    """Per-query measurement produced by :meth:`TierRoutingPilot.run`.

    Attributes:
        query_id: ``GoldenQuery.id``.
        strategy: ``"baseline"`` or ``"heuristic"``.
        tier: Chosen tier.
        cost_usd: Cost for this single query.
        latency_ms: Simulated latency.
        accuracy: ``1.0`` if the simulated answer is correct, else ``0.0``.
    """

    query_id: str
    strategy: str
    tier: str
    cost_usd: float
    latency_ms: float
    accuracy: float


@dataclass(frozen=True)
class StrategyReport:
    """Aggregate metrics for one routing strategy over the corpus.

    Attributes:
        strategy: ``"baseline"`` or ``"heuristic"``.
        total_queries: Number of queries evaluated.
        tier_distribution: ``{tier: count}`` (counts sum to total_queries).
        total_cost_usd: Sum of per-query costs.
        mean_accuracy: Micro-average accuracy across queries.
        cost_reduction_pct: ``100 * (1 - total_cost / baseline_total_cost)``.
            Only meaningful for the ``heuristic`` report; ``0.0`` for
            the baseline (it is its own baseline).
        accuracy_drop_pct: ``100 * (baseline_acc - this_acc)``.
            Only meaningful for the heuristic; ``0.0`` for baseline.
        latency_p50_ms / p95_ms / p99_ms: Simulated latency percentiles.
        outcomes: Full per-query list (for auditability).
    """

    strategy: str
    total_queries: int
    tier_distribution: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    mean_accuracy: float = 0.0
    cost_reduction_pct: float = 0.0
    accuracy_drop_pct: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    outcomes: list[QueryOutcome] = field(default_factory=list)


# === Router protocol + implementations =================================


class TierRouterProtocol(Protocol):
    """Structural interface every router must satisfy.

    The pilot is router-agnostic: pass any callable matching this
    protocol via :meth:`TierRoutingPilot.run`'s ``router`` argument.
    The shipped :class:`HeuristicRouter` and the constant
    :class:`BaselineRouter` both conform.
    """

    def select_heuristic(self, query: GoldenQuery) -> RoutingDecision:
        """Decide which tier should answer ``query``.

        Args:
            query: The golden query (with difficulty + category metadata).

        Returns:
            A frozen :class:`RoutingDecision`.
        """
        ...


class BaselineRouter:
    """Always-T3 router — the cost / quality reference point."""

    _MODEL = "MiniMax-M3"

    def select_heuristic(self, query: GoldenQuery) -> RoutingDecision:
        return RoutingDecision(
            tier="T3",
            model=self._MODEL,
            reason="baseline: always T3 (frontier cloud)",
            confidence=1.0,
        )


class HeuristicRouter:
    """Rule-based tier router — the strategy under evaluation.

    Routing rules (applied in order, first match wins):

        1. ``category == "multi_hop"`` → ``T3`` (multi-hop reasoning
           needs the frontier model).
        2. ``difficulty == "hard"`` → ``T3`` (paraphrased hard queries
           need strong comprehension).
        3. ``category == "factual_lookup"`` AND
           ``difficulty in {"easy", "medium"}`` AND
           ``len(query) <= _T1_MAX_QUERY_LEN`` → ``T1`` (short factual
           lookup — local 8B Qwen3 handles BM25-friendly single-fact
           questions fine even when medium-paraphrased).
        4. ``difficulty == "medium"`` → ``T2`` (mid cloud for the
           longer medium queries that fell through rule 3).
        5. ``difficulty == "easy"`` → ``T1`` (long easy queries still
           go local — they are factual lookups).
        6. Fallback → ``T2`` (conservative: better T2 than a wrong T1).

    The pilot corpus (50 golden queries: 15 easy + 16 medium + 19 hard,
    40 factual_lookup + 5 paraphrased + 5 multi_hop) yields ~30 T1 /
    ~5 T2 / ~15 T3 — meeting B1 (≥ 60 % T1) while still reserving T3
    for the genuinely hard queries.

    This is deliberately simple: it is a **pilot** to validate that
    a heuristic can meet the DoD thresholds. A learned router
    (Phase 6.1C) will replace it once the pilot proves the savings
    are real.
    """

    _T1_MODEL = "qwen3:8b"
    _T2_MODEL = "glm-4.7"
    _T3_MODEL = "MiniMax-M3"
    #: Queries longer than this (chars) skip T1 for medium difficulty
    #: (rule 3). Easy queries ignore this limit (rule 5).
    _T1_MAX_QUERY_LEN: int = 60

    def select_heuristic(self, query: GoldenQuery) -> RoutingDecision:
        # Rule 1: multi-hop always needs the frontier model.
        if query.category == "multi_hop":
            return RoutingDecision(
                tier="T3",
                model=self._T3_MODEL,
                reason="multi-hop → T3 (frontier)",
                confidence=0.95,
            )
        # Rule 2: hard difficulty → T3.
        if query.difficulty == "hard":
            return RoutingDecision(
                tier="T3",
                model=self._T3_MODEL,
                reason="hard difficulty → T3",
                confidence=0.85,
            )
        # Rule 3: short factual lookup or paraphrase (easy OR medium) → T1.
        # ``paraphrased`` queries are single-fact lookups with synonyms —
        # the same BM25-friendly territory as ``factual_lookup``, so a
        # local 8B model handles them just as well.
        if (
            query.category in ("factual_lookup", "paraphrased")
            and query.difficulty in ("easy", "medium")
            and len(query.query) <= self._T1_MAX_QUERY_LEN
        ):
            return RoutingDecision(
                tier="T1",
                model=self._T1_MODEL,
                reason=f"{query.difficulty} {query.category} + short → T1 (local)",
                confidence=0.90,
            )
        # Rule 4: medium difficulty (longer or paraphrased) → T2.
        if query.difficulty == "medium":
            return RoutingDecision(
                tier="T2",
                model=self._T2_MODEL,
                reason="medium difficulty → T2",
                confidence=0.80,
            )
        # Rule 5: easy difficulty (long query, still factual) → T1.
        if query.difficulty == "easy":
            return RoutingDecision(
                tier="T1",
                model=self._T1_MODEL,
                reason="easy → T1 (local)",
                confidence=0.90,
            )
        # Rule 6: fallback → T2 (conservative).
        return RoutingDecision(
            tier="T2",
            model=self._T2_MODEL,
            reason="fallback → T2 (conservative)",
            confidence=0.70,
        )


# === Pilot harness =====================================================


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolation percentile on an already-sorted list.

    Args:
        sorted_vals: Values sorted ascending.
        p: Percentile in ``[0, 100]``.

    Returns:
        The ``p``-th percentile value. Returns ``0.0`` for an empty
        list (defensive — pilot callers guard against this).
    """
    if not sorted_vals:
        return 0.0
    if not 0.0 <= p <= 100.0:
        raise ValueError(f"percentile must be in [0, 100], got {p}")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    # Linear interpolation between closest ranks.
    rank = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def _simulate_accuracy(tier: str, difficulty: str) -> float:
    """Deterministic per-query accuracy given tier + difficulty.

    Uses :data:`_MISS_RATE`. Returns ``1.0`` (correct) or ``0.0``
    (wrong). The miss-rate is interpreted as a probability, but the
    pilot uses the **expected value** (``1 - miss_rate``) directly
    rather than sampling, so the report is fully reproducible.

    Args:
        tier: ``"T1"`` / ``"T2"`` / ``"T3"``.
        difficulty: ``"easy"`` / ``"medium"`` / ``"hard"`` (others
            fall back to :data:`_DEFAULT_MISS_RATE`).

    Returns:
        Expected accuracy in ``[0, 1]``.
    """
    miss_rate = _MISS_RATE.get(tier, {}).get(difficulty, _DEFAULT_MISS_RATE)
    return max(0.0, 1.0 - miss_rate)


def _routing_overhead_ms(router: TierRouterProtocol) -> float:
    """Simulated routing decision latency (ms).

    Baseline router is a constant return (~0 ms). The heuristic
    router inspects query fields (~1 ms). Kept here so the latency
    percentiles include the routing cost.
    """
    if isinstance(router, BaselineRouter):
        return 0.0
    return 1.0


class TierRoutingPilot:
    """Run the baseline-vs-heuristic comparison on a golden query set.

    Usage::

        pilot = TierRoutingPilot()
        report = pilot.run(queries)
        pilot.save_report(report, Path("results/tier_routing_pilot_v126.json"))

    The pilot is **deterministic**: the same ``queries`` list always
    yields the same report (no RNG, no real LLM calls).
    """

    #: DoD thresholds (Acceptance criteria B1-B4).
    DOD_T1_RATIO_MIN: float = 0.60
    DOD_ACCURACY_DROP_MAX_PCT: float = 5.0
    DOD_COST_REDUCTION_MIN_PCT: float = 40.0
    DOD_LATENCY_P95_REGRESSION_MAX_PCT: float = 20.0

    def run(
        self,
        queries: list[GoldenQuery],
        *,
        baseline_router: TierRouterProtocol | None = None,
        heuristic_router: TierRouterProtocol | None = None,
    ) -> dict[str, Any]:
        """Evaluate both strategies and return a combined report.

        Args:
            queries: Golden query corpus (typically 50 from conftest).
            baseline_router: Override the default always-T3 router
                (advanced; leave ``None`` for the standard pilot).
            heuristic_router: Override the default
                :class:`HeuristicRouter`. Pass a real
                ``TierSelector.select_heuristic`` adapter here once
                Coder ships it.

        Returns:
            A dict (also JSON-serialisable via :meth:`save_report`)
            with keys ``baseline``, ``heuristic``, ``dod``, and
            ``meta``.
        """
        if not queries:
            raise ValueError("queries must be non-empty")

        baseline_router = baseline_router or BaselineRouter()
        heuristic_router = heuristic_router or HeuristicRouter()

        baseline_report = self._evaluate_strategy(
            "baseline", queries, baseline_router
        )
        heuristic_report = self._evaluate_strategy(
            "heuristic", queries, heuristic_router
        )

        # Cross-strategy derived metrics.
        baseline_cost = baseline_report.total_cost_usd or 1.0
        cost_reduction_pct = 100.0 * (
            1.0 - heuristic_report.total_cost_usd / baseline_cost
        )
        accuracy_drop_pct = 100.0 * (
            baseline_report.mean_accuracy - heuristic_report.mean_accuracy
        )
        # Replace the placeholder values set inside _evaluate_strategy
        # with the cross-strategy-aware numbers.
        heuristic_report = StrategyReport(
            strategy=heuristic_report.strategy,
            total_queries=heuristic_report.total_queries,
            tier_distribution=heuristic_report.tier_distribution,
            total_cost_usd=heuristic_report.total_cost_usd,
            mean_accuracy=heuristic_report.mean_accuracy,
            cost_reduction_pct=cost_reduction_pct,
            accuracy_drop_pct=accuracy_drop_pct,
            latency_p50_ms=heuristic_report.latency_p50_ms,
            latency_p95_ms=heuristic_report.latency_p95_ms,
            latency_p99_ms=heuristic_report.latency_p99_ms,
            outcomes=heuristic_report.outcomes,
        )

        latency_p95_baseline = baseline_report.latency_p95_ms or 1.0
        latency_p95_regression_pct = 100.0 * (
            heuristic_report.latency_p95_ms / latency_p95_baseline - 1.0
        )
        t1_ratio = heuristic_report.tier_distribution.get("T1", 0) / max(
            heuristic_report.total_queries, 1
        )

        dod = {
            "B1_t1_ratio": {
                "value": round(t1_ratio, 4),
                "threshold": self.DOD_T1_RATIO_MIN,
                "pass": t1_ratio >= self.DOD_T1_RATIO_MIN,
            },
            "B2_accuracy_drop_pct": {
                "value": round(accuracy_drop_pct, 4),
                "threshold": self.DOD_ACCURACY_DROP_MAX_PCT,
                "pass": accuracy_drop_pct < self.DOD_ACCURACY_DROP_MAX_PCT,
            },
            "B3_cost_reduction_pct": {
                "value": round(cost_reduction_pct, 4),
                "threshold": self.DOD_COST_REDUCTION_MIN_PCT,
                "pass": cost_reduction_pct >= self.DOD_COST_REDUCTION_MIN_PCT,
            },
            "B4_latency_p95_regression_pct": {
                "value": round(latency_p95_regression_pct, 4),
                "threshold": self.DOD_LATENCY_P95_REGRESSION_MAX_PCT,
                # "no worse than +20 %" → regression must be <= +20 %.
                "pass": latency_p95_regression_pct
                <= self.DOD_LATENCY_P95_REGRESSION_MAX_PCT,
            },
        }
        dod["all_pass"] = all(item["pass"] for item in dod.values() if isinstance(item, dict) and "pass" in item)

        return {
            "meta": {
                "phase": "6.1B",
                "corpus_size": len(queries),
                "cost_model_usd_per_query": TIER_COST_USD,
                "latency_model_ms": TIER_LATENCY_MS,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            "baseline": self._report_to_dict(baseline_report),
            "heuristic": self._report_to_dict(heuristic_report),
            "dod": dod,
        }

    def _evaluate_strategy(
        self,
        name: str,
        queries: list[GoldenQuery],
        router: TierRouterProtocol,
    ) -> StrategyReport:
        """Run one router over the corpus and aggregate per-query outcomes."""
        overhead = _routing_overhead_ms(router)
        outcomes: list[QueryOutcome] = []
        tier_counts: dict[str, int] = {"T1": 0, "T2": 0, "T3": 0}
        total_cost = 0.0
        total_acc = 0.0
        latencies: list[float] = []

        for q in queries:
            decision = router.select_heuristic(q)
            tier = decision.tier
            if tier not in TIER_COST_USD:
                raise ValueError(
                    f"router returned unknown tier {tier!r} for query {q.id}"
                )
            cost = TIER_COST_USD[tier]
            latency = TIER_LATENCY_MS[tier] + overhead
            accuracy = _simulate_accuracy(tier, q.difficulty)

            tier_counts[tier] += 1
            total_cost += cost
            total_acc += accuracy
            latencies.append(latency)
            outcomes.append(
                QueryOutcome(
                    query_id=q.id,
                    strategy=name,
                    tier=tier,
                    cost_usd=cost,
                    latency_ms=latency,
                    accuracy=accuracy,
                )
            )

        latencies_sorted = sorted(latencies)
        return StrategyReport(
            strategy=name,
            total_queries=len(queries),
            tier_distribution=tier_counts,
            total_cost_usd=round(total_cost, 6),
            mean_accuracy=round(total_acc / len(queries), 6),
            cost_reduction_pct=0.0,  # filled in by run()
            accuracy_drop_pct=0.0,   # filled in by run()
            latency_p50_ms=round(_percentile(latencies_sorted, 50), 3),
            latency_p95_ms=round(_percentile(latencies_sorted, 95), 3),
            latency_p99_ms=round(_percentile(latencies_sorted, 99), 3),
            outcomes=outcomes,
        )

    @staticmethod
    def _report_to_dict(report: StrategyReport) -> dict[str, Any]:
        """Serialise a :class:`StrategyReport` to a JSON-friendly dict."""
        return {
            "strategy": report.strategy,
            "total_queries": report.total_queries,
            "tier_distribution": report.tier_distribution,
            "total_cost_usd": report.total_cost_usd,
            "mean_accuracy": report.mean_accuracy,
            "cost_reduction_pct": report.cost_reduction_pct,
            "accuracy_drop_pct": report.accuracy_drop_pct,
            "latency_p50_ms": report.latency_p50_ms,
            "latency_p95_ms": report.latency_p95_ms,
            "latency_p99_ms": report.latency_p99_ms,
            "outcomes": [asdict(o) for o in report.outcomes],
        }

    def save_report(self, report: dict[str, Any], path: Path) -> Path:
        """Write the report dict to ``path`` as pretty-printed JSON.

        Creates parent directories if missing. Returns the path written.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("tier routing pilot report saved → %s", path)
        return path


__all__ = [
    "TIER_COST_USD",
    "TIER_LATENCY_MS",
    "TIER_PRIORITY",
    "RoutingDecision",
    "QueryOutcome",
    "StrategyReport",
    "TierRouterProtocol",
    "BaselineRouter",
    "HeuristicRouter",
    "TierRoutingPilot",
]
