"""Phase 3 B-mini + Phase 5 B2/B3: EvalRunner — async orchestrator.

C6 fix: this is **async** (not sync) because ``ContextCompactor.maybe_compact``
is async. Mirrors the pattern in ``tests/test_context_compaction.py``:
``@pytest.mark.asyncio`` async tests.

Phase 5 (16.06.2026) adds ``run_precision`` and ``run_recall`` for B2/B3
retrieval metrics. These are **sync** (the metrics themselves are sync)
but wrapped in async methods to keep the runner interface uniform.

Usage::

    runner = EvalRunner()
    retention_result = await runner.run_retention(session, facts, top_k=20)
    loss_result = await runner.run_compaction_loss(session, facts, compactor, "qwen3:8b")
    precision_result = await runner.run_precision(corpus, queries, facts, k=5)
    recall_result = await runner.run_recall(corpus, queries, facts, k=20)
"""
from __future__ import annotations

from typing import Union

from harness.eval.compaction_loss import CompactionLossMetric, LossResult
from harness.eval.golden import GoldenFact, GoldenQuery
from harness.eval.retention import ContextRetentionMetric, RetentionResult
from harness.eval.retrieval import (
    PrecisionMetric,
    PrecisionResult,
    RecallMetric,
    RecallResult,
    session_to_corpus,
)
from harness.memory.schema import Memory

# Phase 5.2A v1.24.0: corpus may be a channel-separated dict OR a
# legacy flat list. Both metric classes accept either shape.
CorpusInput = Union[dict[str, list[Memory]], list[Memory]]


class EvalRunner:
    """Async orchestrator for Phase 3 B-mini + Phase 5 B2/B3 metrics.

    Wraps the four metric classes in a single fixture-driven entry
    point. Adding a new metric (e.g. Phase 5.1 hybrid retrieval) is
    a single method addition.
    """

    def __init__(self) -> None:
        self._retention = ContextRetentionMetric()
        self._loss = CompactionLossMetric()

    async def run_retention(
        self,
        session: list[dict],
        facts: list[GoldenFact],
        top_k: int = 20,
    ) -> RetentionResult:
        """Run B1 — context retention."""
        metric = ContextRetentionMetric(top_k=top_k)
        return metric.measure(session, facts)

    async def run_compaction_loss(
        self,
        session: list[dict],
        facts: list[GoldenFact],
        compactor,  # ContextCompactor — no import to keep trust boundary
        model_name: str,
    ) -> LossResult:
        """Run B4 — compaction loss."""
        return await self._loss.measure(session, facts, compactor, model_name)

    async def run_precision(
        self,
        corpus: CorpusInput,
        queries: list[GoldenQuery],
        facts: list[GoldenFact],
        k: int = 5,
        threshold_target: float = 0.7,
        *,
        channels: list[str] | None = None,
    ) -> PrecisionResult:
        """Run B2 — precision@k (default k=5).

        Sync metric wrapped in async for runner interface consistency.

        Phase 5.2A v1.24.0: ``corpus`` may be a channel-separated dict
        (from ``session_to_corpus``) or a legacy flat list. ``channels``
        optionally filters which channels contribute to the BM25 corpus.
        """
        metric = PrecisionMetric(
            k=k, threshold_target=threshold_target, channels=channels,
        )
        return metric.measure(corpus, queries, facts)

    async def run_recall(
        self,
        corpus: CorpusInput,
        queries: list[GoldenQuery],
        facts: list[GoldenFact],
        k: int = 20,
        threshold_target: float = 0.85,
        *,
        channels: list[str] | None = None,
    ) -> RecallResult:
        """Run B3 — recall@k (default k=20).

        Sync metric wrapped in async for runner interface consistency.

        Phase 5.2A v1.24.0: ``corpus`` may be a channel-separated dict
        (from ``session_to_corpus``) or a legacy flat list. ``channels``
        optionally filters which channels contribute to the BM25 corpus.
        """
        metric = RecallMetric(
            k=k, threshold_target=threshold_target, channels=channels,
        )
        return metric.measure(corpus, queries, facts)


__all__ = ["EvalRunner"]
