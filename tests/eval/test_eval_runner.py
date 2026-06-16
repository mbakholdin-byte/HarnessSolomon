"""Phase 5 B2/B3: EvalRunner integration tests.

Verify the ``EvalRunner.run_precision()`` and ``EvalRunner.run_recall()``
async wrappers work end-to-end and return the same results as the
direct metric calls.

Test scope (3 tests):
    - test_runner_run_precision_matches_direct: runner wrapper returns
      the same PrecisionResult as direct PrecisionMetric call.
    - test_runner_run_recall_matches_direct: same for recall.
    - test_runner_preserves_4_metrics: runner exposes all 4 metrics
      (run_retention, run_compaction_loss, run_precision, run_recall).
"""
from __future__ import annotations

import pytest

from harness.eval import EvalRunner, RecallMetric
from harness.eval.retrieval import PrecisionMetric, session_to_corpus


@pytest.mark.asyncio
async def test_runner_run_precision_matches_direct(
    seed_session_100: list[dict],
    golden_queries,
    golden_facts,
) -> None:
    """``EvalRunner.run_precision`` returns same as ``PrecisionMetric.measure``."""
    corpus = session_to_corpus(seed_session_100)
    runner = EvalRunner()

    direct = PrecisionMetric(k=5).measure(corpus, golden_queries, golden_facts)
    via_runner = await runner.run_precision(corpus, golden_queries, golden_facts, k=5)

    assert via_runner.threshold_ratio == direct.threshold_ratio
    assert via_runner.threshold_top5 == direct.threshold_top5
    assert via_runner.threshold_relevant_in_top5 == direct.threshold_relevant_in_top5
    assert via_runner.k == direct.k == 5


@pytest.mark.asyncio
async def test_runner_run_recall_matches_direct(
    seed_session_100: list[dict],
    golden_queries,
    golden_facts,
) -> None:
    """``EvalRunner.run_recall`` returns same as ``RecallMetric.measure``."""
    corpus = session_to_corpus(seed_session_100)
    runner = EvalRunner()

    direct = RecallMetric(k=20).measure(corpus, golden_queries, golden_facts)
    via_runner = await runner.run_recall(corpus, golden_queries, golden_facts, k=20)

    assert via_runner.threshold_ratio == direct.threshold_ratio
    assert via_runner.threshold_relevant_in_ground_truth == direct.threshold_relevant_in_ground_truth
    assert via_runner.threshold_relevant_retrieved == direct.threshold_relevant_retrieved
    assert via_runner.k == direct.k == 20


def test_runner_preserves_4_metrics() -> None:
    """EvalRunner exposes all 4 run_* methods (B1, B2, B3, B4)."""
    runner = EvalRunner()
    assert hasattr(runner, "run_retention")
    assert hasattr(runner, "run_compaction_loss")
    assert hasattr(runner, "run_precision")
    assert hasattr(runner, "run_recall")
    # All should be callable.
    assert callable(runner.run_retention)
    assert callable(runner.run_compaction_loss)
    assert callable(runner.run_precision)
    assert callable(runner.run_recall)
