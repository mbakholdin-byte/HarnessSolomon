"""Phase 3 B-mini: EvalRunner — async orchestrator.

C6 fix: this is **async** (not sync) because ``ContextCompactor.maybe_compact``
is async. Mirrors the pattern in ``tests/test_context_compaction.py``:
``@pytest.mark.asyncio`` async tests.

Usage::

    runner = EvalRunner()
    retention_result = await runner.run_retention(session, facts, top_k=20)
    loss_result = await runner.run_compaction_loss(session, facts, compactor, "qwen3:8b")
"""
from __future__ import annotations

from harness.eval.compaction_loss import CompactionLossMetric, LossResult
from harness.eval.golden import GoldenFact
from harness.eval.retention import ContextRetentionMetric, RetentionResult


class EvalRunner:
    """Async orchestrator for the Phase 3 B-mini metrics.

    Wraps the two metric classes in a single fixture-driven entry point.
    Future metrics (B2 precision@5, B3 recall@20) will be added here
    without changing the runner's public surface.
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


__all__ = ["EvalRunner"]
