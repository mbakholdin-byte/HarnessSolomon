"""Phase 6.1B — tests for the Tier Router pilot eval.

Four core DoD tests + structural sanity tests. Uses the same
``golden_queries`` fixture (50 queries: 30 auto + 20 manual) as the
Phase 5 B2/B3 suite, so the pilot runs against the real corpus.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.eval.golden import GoldenQuery
from harness.eval.tier_routing import (
    BaselineRouter,
    HeuristicRouter,
    TIER_COST_USD,
    TIER_LATENCY_MS,
    TierRoutingPilot,
)


# === Fixtures ==========================================================


@pytest.fixture
def pilot() -> TierRoutingPilot:
    return TierRoutingPilot()


@pytest.fixture
def report(pilot: TierRoutingPilot, golden_queries: list[GoldenQuery]) -> dict:
    """Full pilot report over the 50-query golden corpus."""
    return pilot.run(golden_queries)


# === B1: heuristic routes majority to T1 ===============================


def test_heuristic_routes_majority_to_t1(report: dict) -> None:
    """B1 — ≥ 60 % of queries must route to T1 under the heuristic."""
    heur = report["heuristic"]
    total = heur["total_queries"]
    t1_count = heur["tier_distribution"].get("T1", 0)
    t1_ratio = t1_count / total
    assert t1_ratio >= 0.60, (
        f"B1 fail: T1 ratio {t1_ratio:.2%} < 60 % "
        f"(T1={t1_count}/{total}, distribution={heur['tier_distribution']})"
    )


def test_heuristic_router_directly_easy_short_to_t1() -> None:
    """Unit-level: an easy, short factual lookup → T1."""
    q = GoldenQuery(
        id="T-EASY",
        query="what is Qdrant primary store",  # < 60 chars
        relevant_fact_ids=("F02",),
        irrelevant_fact_ids=("F01",),
        category="factual_lookup",
        difficulty="easy",
    )
    decision = HeuristicRouter().select_heuristic(q)
    assert decision.tier == "T1", f"expected T1, got {decision.tier} ({decision.reason})"


def test_heuristic_router_multi_hop_to_t3() -> None:
    """Multi-hop always → T3 regardless of difficulty."""
    q = GoldenQuery(
        id="T-MH",
        query="how does X relate to Y and Z combined",
        relevant_fact_ids=("F01", "F02"),
        irrelevant_fact_ids=("F03",),
        category="multi_hop",
        difficulty="hard",
    )
    decision = HeuristicRouter().select_heuristic(q)
    assert decision.tier == "T3"


def test_heuristic_router_hard_to_t3() -> None:
    """Hard difficulty (non-multi-hop) → T3."""
    q = GoldenQuery(
        id="T-HARD",
        query="summarise the architecture of BM25 k1 1.5 b 0.75 design",
        relevant_fact_ids=("F22",),
        irrelevant_fact_ids=("F01",),
        category="factual_lookup",
        difficulty="hard",
    )
    assert HeuristicRouter().select_heuristic(q).tier == "T3"


def test_heuristic_router_medium_to_t2() -> None:
    """Medium difficulty with a long query → T2 (falls through rule 3)."""
    long_query = (
        "how is PrivacyZoneFilter default patterns configured "
        "across the various subsystems and layers of the harness"
    )
    assert len(long_query) > 60  # sanity: triggers rule 4, not rule 3
    q = GoldenQuery(
        id="T-MED",
        query=long_query,
        relevant_fact_ids=("F13",),
        irrelevant_fact_ids=("F01",),
        category="factual_lookup",
        difficulty="medium",
    )
    assert HeuristicRouter().select_heuristic(q).tier == "T2"


def test_heuristic_router_medium_short_factual_to_t1() -> None:
    """Medium factual + short query → T1 (rule 3, the B1 enabler)."""
    q = GoldenQuery(
        id="T-MEDSHORT",
        query="how is BM25 k1 1.5 b 0.75 configured",  # < 60 chars
        relevant_fact_ids=("F22",),
        irrelevant_fact_ids=("F01",),
        category="factual_lookup",
        difficulty="medium",
    )
    assert HeuristicRouter().select_heuristic(q).tier == "T1"


# === B2: accuracy drop within bounds ===================================


def test_accuracy_drop_within_bounds(report: dict) -> None:
    """B2 — heuristic accuracy drop < 5 % vs baseline."""
    drop = report["dod"]["B2_accuracy_drop_pct"]
    assert drop["value"] < drop["threshold"], (
        f"B2 fail: accuracy drop {drop['value']:.2f} % "
        f">= threshold {drop['threshold']} %"
    )


def test_baseline_accuracy_is_perfect(report: dict) -> None:
    """Baseline (always T3) should be ~1.0 accuracy by construction."""
    assert report["baseline"]["mean_accuracy"] >= 0.999, (
        f"baseline accuracy {report['baseline']['mean_accuracy']} "
        f"should be ~1.0 (T3 has 0 miss-rate)"
    )


# === B3: cost reduction meets threshold ================================


def test_cost_reduction_meets_threshold(report: dict) -> None:
    """B3 — cost reduction ≥ 40 % vs always-T3 baseline."""
    reduction = report["dod"]["B3_cost_reduction_pct"]
    assert reduction["value"] >= reduction["threshold"], (
        f"B3 fail: cost reduction {reduction['value']:.2f} % "
        f"< threshold {reduction['threshold']} %"
    )


def test_baseline_cost_is_max_possible(report: dict) -> None:
    """Baseline cost = n_queries * T3 cost (sanity)."""
    n = report["baseline"]["total_queries"]
    expected = n * TIER_COST_USD["T3"]
    assert abs(report["baseline"]["total_cost_usd"] - expected) < 1e-9


# === B4: latency p95 no regression =====================================


def test_latency_p95_no_regression(report: dict) -> None:
    """B4 — heuristic p95 latency not worse than +20 % vs baseline."""
    reg = report["dod"]["B4_latency_p95_regression_pct"]
    assert reg["value"] <= reg["threshold"], (
        f"B4 fail: p95 regression {reg['value']:.2f} % "
        f"> threshold +{reg['threshold']} %"
    )


def test_baseline_latency_p95_is_t3(report: dict) -> None:
    """Baseline p95 ≈ T3 latency (all queries → T3)."""
    expected = TIER_LATENCY_MS["T3"]
    assert abs(report["baseline"]["latency_p95_ms"] - expected) < 1e-6


# === Structural / sanity tests =========================================


def test_baseline_always_t3_routes_correctly(
    golden_queries: list[GoldenQuery],
) -> None:
    """BaselineRouter must route every single query to T3."""
    router = BaselineRouter()
    for q in golden_queries:
        decision = router.select_heuristic(q)
        assert decision.tier == "T3", (
            f"baseline routed {q.id} to {decision.tier}, expected T3"
        )
        assert decision.model == "MiniMax-M3"


def test_report_is_json_serialisable(report: dict, tmp_path: Path) -> None:
    """The full report must round-trip through JSON."""
    path = tmp_path / "pilot.json"
    TierRoutingPilot().save_report(report, path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["meta"]["phase"] == "6.1B"
    assert loaded["dod"]["all_pass"] in (True, False)


def test_all_outcomes_accounted(report: dict) -> None:
    """Every strategy's outcomes count must equal total_queries."""
    for strategy in ("baseline", "heuristic"):
        rep = report[strategy]
        assert len(rep["outcomes"]) == rep["total_queries"]
        counts = {"T1": 0, "T2": 0, "T3": 0}
        for o in rep["outcomes"]:
            counts[o["tier"]] += 1
        assert counts == rep["tier_distribution"], (
            f"{strategy}: outcome tier counts {counts} != "
            f"distribution {rep['tier_distribution']}"
        )


def test_dod_structure(report: dict) -> None:
    """The DoD block must contain all 4 criteria + all_pass."""
    keys = set(report["dod"].keys())
    expected = {
        "B1_t1_ratio",
        "B2_accuracy_drop_pct",
        "B3_cost_reduction_pct",
        "B4_latency_p95_regression_pct",
        "all_pass",
    }
    assert keys == expected, f"DoD keys mismatch: {keys ^ expected}"


def test_empty_queries_raises() -> None:
    """Pilot must reject an empty corpus (no silent zero-division)."""
    with pytest.raises(ValueError, match="non-empty"):
        TierRoutingPilot().run([])
