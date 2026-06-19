"""Phase 5 B3: recall@20 on golden queries.

B3 DoD: ``RecallMetric.measure(corpus, queries, facts).threshold_ratio``
≥ 0.85 on the **subset** of 40 factual_lookup + paraphrased queries
(multi_hop reported separately in ``per_category``).

Test scope (5 tests):
    - test_b3_recall_baseline: 50-query run, ``threshold_ratio`` is
      computed and well-formed.
    - test_b3_recall_subset_excludes_multihop: multi_hop NOT in
      threshold ratio, but IS in per_category.
    - test_b3_recall_empty_queries_returns_one: empty input → 1.0.
    - test_b3_recall_k_configurable: k=5 vs k=20 differ.
    - test_b3_recall_threshold_target_field: result carries target.
"""
from __future__ import annotations

import pytest

from harness.eval import (
    GoldenFact,
    GoldenQuery,
    RecallMetric,
)
from harness.eval.retrieval import session_to_corpus


def test_b3_recall_baseline(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
) -> None:
    """B3 baseline: recall@20 on 50 golden queries.

    Threshold scope (Phase 5 B2/B3 sign-off 2026-06-16):
      - 45 threshold queries (30 auto + 10 manual factual_lookup +
        5 manual paraphrased). 5 manual multi_hop excluded.
      - Some manual queries have 2 relevant fact_ids (Q01, Q03,
        Q05, Q10, Q11, Q12), so the denominator is the SUM of
        |gt| across all 45 threshold queries. Auto is always 1-fact.
    """
    corpus = session_to_corpus(seed_session_100)
    metric = RecallMetric(k=20, threshold_target=0.85)
    result = metric.measure(corpus, golden_queries, golden_facts)

    assert result.k == 20
    assert result.threshold_target == 0.85
    assert 0.0 <= result.threshold_ratio <= 1.0
    # Auto: 30 × 1 = 30. Manual factual: 6 × 1 + 4 × 2 = 14.
    # Manual paraphrased: 3 × 1 + 2 × 2 = 7. Total = 51.
    assert result.threshold_relevant_in_ground_truth == 51
    assert len(result.per_query) >= 49


def test_b3_recall_subset_excludes_multihop(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
) -> None:
    """Multi-hop queries are reported in per_category, NOT in threshold."""
    corpus = session_to_corpus(seed_session_100)
    metric = RecallMetric(k=20)
    result = metric.measure(corpus, golden_queries, golden_facts)

    assert "multi_hop" in result.per_category
    assert "factual_lookup" in result.per_category
    assert "paraphrased" in result.per_category

    missed_ids = {q.id for q in result.missed}
    multihop_ids = {q.id for q in golden_queries if q.category == "multi_hop"}
    assert missed_ids.isdisjoint(multihop_ids), (
        f"multi_hop queries leaked into threshold missed: "
        f"{missed_ids & multihop_ids}"
    )


def test_b3_recall_empty_queries_returns_one() -> None:
    """Empty queries list → ratio = 1.0."""
    metric = RecallMetric(k=20)
    result = metric.measure([], [], [])

    assert result.threshold_ratio == 1.0
    assert result.threshold_relevant_in_ground_truth == 0
    assert result.threshold_relevant_retrieved == 0


def test_b3_recall_k_configurable(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
) -> None:
    """k=5 returns different result than k=20."""
    corpus = session_to_corpus(seed_session_100)
    metric5 = RecallMetric(k=5)
    metric20 = RecallMetric(k=20)

    r5 = metric5.measure(corpus, golden_queries, golden_facts)
    r20 = metric20.measure(corpus, golden_queries, golden_facts)

    assert r5.k == 5
    assert r20.k == 20
    # Recall@5 should be ≤ recall@20 (more docs available at k=20).
    assert r5.threshold_ratio <= r20.threshold_ratio + 1e-9


def test_b3_recall_threshold_target_field() -> None:
    """Result dataclass carries the configured threshold_target."""
    metric = RecallMetric(k=20, threshold_target=0.9)
    result = metric.measure([], [], [])

    assert result.threshold_target == 0.9
    assert result.k == 20


def test_b3_recall_rejects_invalid_k() -> None:
    """Constructor rejects k <= 0."""
    with pytest.raises(ValueError, match="k must be > 0"):
        RecallMetric(k=0)


def test_b3_recall_rejects_invalid_threshold() -> None:
    """Constructor rejects threshold_target outside [0, 1]."""
    with pytest.raises(ValueError, match="threshold_target"):
        RecallMetric(k=20, threshold_target=1.1)


def test_b3_recall_rejects_k_above_corpus(
    seed_session_100: list[dict],
    golden_facts: list[GoldenFact],
) -> None:
    """measure() raises if k > corpus size AND queries is non-empty."""
    from harness.eval.retrieval import flatten_corpus

    corpus = session_to_corpus(seed_session_100)
    flat = flatten_corpus(corpus)
    metric = RecallMetric(k=len(flat) + 1)
    q = GoldenQuery(
        id="Q_TEST", query="x", relevant_fact_ids=("F01",),
        irrelevant_fact_ids=(), category="factual_lookup", difficulty="easy",
    )
    with pytest.raises(ValueError, match="exceeds corpus size"):
        metric.measure(corpus, [q], golden_facts)
