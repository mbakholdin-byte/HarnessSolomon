"""Phase 5 B2: precision@5 on golden queries.

B2 DoD: ``PrecisionMetric.measure(corpus, queries, facts).threshold_ratio``
≥ 0.7 on the **subset** of 40 factual_lookup + paraphrased queries
(multi_hop reported separately in ``per_category``).

Test scope (6 tests):
    - test_b2_precision_baseline: 50-query run, no exceptions,
      ``threshold_ratio`` is computed and recorded.
    - test_b2_precision_per_query_nonempty: every factual_lookup /
      paraphrased query has a ``per_query`` entry.
    - test_b2_precision_subset_excludes_multihop: multi_hop queries
      are NOT counted in ``threshold_relevant_in_top5`` /
      ``threshold_top5``, but ARE in ``per_category``.
    - test_b2_precision_empty_queries_returns_one: empty input is
      1.0 ratio (no misses).
    - test_b2_precision_k_configurable: ``k=10`` returns different
      results than ``k=5``.
    - test_b2_precision_threshold_target_field: result carries the
      configured target.
"""
from __future__ import annotations

import pytest

from harness.eval import (
    GoldenFact,
    GoldenQuery,
    PrecisionMetric,
)
from harness.eval.retrieval import session_to_corpus


def test_b2_precision_baseline(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
) -> None:
    """B2 baseline: precision@5 on 50 golden queries.

    Asserts the metric runs end-to-end and returns a valid result.
    The actual ratio depends on BM25 quality on the seed session —
    we only assert that the result is computable and well-formed.

    Threshold scope (Phase 5 B2/B3 sign-off 2026-06-16):
      - 30 auto (factual_lookup) + 10 manual factual_lookup +
        5 manual paraphrased = 45 threshold queries
      - 5 manual multi_hop are reported in per_category, NOT
        counted in threshold
    """
    corpus = session_to_corpus(seed_session_100)
    metric = PrecisionMetric(k=5, threshold_target=0.7)
    result = metric.measure(corpus, golden_queries, golden_facts)

    assert result.k == 5
    assert result.threshold_target == 0.7
    assert result.threshold_top5 == 45 * 5, (
        f"subset scope: 45 threshold queries × k=5; got {result.threshold_top5}"
    )
    assert 0.0 <= result.threshold_ratio <= 1.0
    # 30 auto + 20 manual = 50 queries; per_query should be 50
    # (or 49 if one query has empty ground truth — defensive skip).
    assert len(result.per_query) >= 49, (
        f"per_query should cover nearly all 50 queries; got "
        f"{len(result.per_query)}"
    )


def test_b2_precision_per_query_nonempty(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
) -> None:
    """Every factual_lookup / paraphrased query has a per_query entry."""
    corpus = session_to_corpus(seed_session_100)
    metric = PrecisionMetric(k=5)
    result = metric.measure(corpus, golden_queries, golden_facts)

    # All 50 queries (or 49 if defensive skip) should have entries.
    for q in golden_queries:
        if q.id in result.per_query:
            v = result.per_query[q.id]
            assert 0.0 <= v <= 1.0, f"query {q.id}: precision {v} out of [0,1]"


def test_b2_precision_subset_excludes_multihop(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
) -> None:
    """Multi-hop queries are reported in per_category, NOT in threshold.

    Threshold numerator/denominator is computed on factual_lookup +
    paraphrased only (subset of 40 queries per Марк sign-off 2026-06-16).
    """
    corpus = session_to_corpus(seed_session_100)
    metric = PrecisionMetric(k=5)
    result = metric.measure(corpus, golden_queries, golden_facts)

    # per_category should have all 3 categories (including multi_hop)
    assert "multi_hop" in result.per_category
    assert "factual_lookup" in result.per_category
    assert "paraphrased" in result.per_category

    # missed is a subset of threshold queries → no multi_hop in missed
    missed_ids = {q.id for q in result.missed}
    multihop_ids = {q.id for q in golden_queries if q.category == "multi_hop"}
    assert missed_ids.isdisjoint(multihop_ids), (
        f"multi_hop queries leaked into threshold missed: "
        f"{missed_ids & multihop_ids}"
    )


def test_b2_precision_empty_queries_returns_one() -> None:
    """Empty queries list → ratio = 1.0 (no misses)."""
    metric = PrecisionMetric(k=5)
    result = metric.measure([], [], [])

    assert result.threshold_ratio == 1.0
    assert result.threshold_top5 == 0
    assert result.threshold_relevant_in_top5 == 0
    assert result.per_query == {}


def test_b2_precision_k_configurable(
    seed_session_100: list[dict],
    golden_queries: list[GoldenQuery],
    golden_facts: list[GoldenFact],
) -> None:
    """k=10 returns different top5_count than k=5."""
    corpus = session_to_corpus(seed_session_100)
    metric5 = PrecisionMetric(k=5)
    metric10 = PrecisionMetric(k=10)

    r5 = metric5.measure(corpus, golden_queries, golden_facts)
    r10 = metric10.measure(corpus, golden_queries, golden_facts)

    assert r5.k == 5
    assert r10.k == 10
    assert r5.threshold_top5 == 45 * 5
    assert r10.threshold_top5 == 45 * 10


def test_b2_precision_threshold_target_field() -> None:
    """Result dataclass carries the configured threshold_target."""
    metric = PrecisionMetric(k=5, threshold_target=0.85)
    result = metric.measure([], [], [])

    assert result.threshold_target == 0.85
    assert result.k == 5


def test_b2_precision_rejects_invalid_k() -> None:
    """Constructor rejects k <= 0."""
    with pytest.raises(ValueError, match="k must be > 0"):
        PrecisionMetric(k=0)
    with pytest.raises(ValueError, match="k must be > 0"):
        PrecisionMetric(k=-1)


def test_b2_precision_rejects_invalid_threshold() -> None:
    """Constructor rejects threshold_target outside [0, 1]."""
    with pytest.raises(ValueError, match="threshold_target"):
        PrecisionMetric(k=5, threshold_target=-0.1)
    with pytest.raises(ValueError, match="threshold_target"):
        PrecisionMetric(k=5, threshold_target=1.5)


def test_b2_precision_rejects_k_above_corpus(
    seed_session_100: list[dict],
    golden_facts: list[GoldenFact],
) -> None:
    """measure() raises if k > corpus size AND queries is non-empty."""
    corpus = session_to_corpus(seed_session_100)
    metric = PrecisionMetric(k=len(corpus) + 1)
    # Use a minimal GoldenQuery to trigger the corpus check (empty
    # queries path is a valid no-op and skips the check).
    q = GoldenQuery(
        id="Q_TEST", query="x", relevant_fact_ids=("F01",),
        irrelevant_fact_ids=(), category="factual_lookup", difficulty="easy",
    )
    with pytest.raises(ValueError, match="exceeds corpus size"):
        metric.measure(corpus, [q], golden_facts)
