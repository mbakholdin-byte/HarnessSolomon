"""Solomon Harness — evaluation harness (Phase 3 metrics, B-mini).

Public API:
    - ``GoldenFact`` — frozen dataclass для marked fact в seed session.
    - ``load_golden_facts`` — JSONL loader.
    - ``load_session_messages`` — JSONL loader для OpenAI-shape session.
    - ``ContextRetentionMetric`` — B1 metric (recall на marked facts через BM25).
    - ``CompactionLossMetric`` — B4 metric (% facts в summary message).
    - ``EvalRunner`` — async orchestrator (fixture-driven, batch).

**Trust boundary:** Этот пакет НЕ импортирует ``harness.agents`` или
``harness.server``. Только ``harness.memory.retrieval.bm25`` (read-only),
``harness.memory.schema`` (``Memory``), ``harness.config`` (``Settings``),
``harness.context`` (``ContextCompactor``), и stdlib. Проверяется
``tests/eval/test_eval_trust_boundary.py`` (parametrized over all .py).
"""
from harness.eval.golden import (
    GoldenFact,
    load_golden_facts,
    load_session_messages,
)
from harness.eval.retention import (
    ContextRetentionMetric,
    RetentionResult,
)
from harness.eval.compaction_loss import (
    CompactionLossMetric,
    LossResult,
)
from harness.eval.runner import EvalRunner

__all__ = [
    "GoldenFact",
    "load_golden_facts",
    "load_session_messages",
    "ContextRetentionMetric",
    "RetentionResult",
    "CompactionLossMetric",
    "LossResult",
    "EvalRunner",
]
