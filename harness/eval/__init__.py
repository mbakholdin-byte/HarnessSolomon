"""Solomon Harness — evaluation harness (Phase 3 metrics, B-mini + Phase 5).

Public API:
    - ``GoldenFact`` — frozen dataclass для marked fact в seed session.
    - ``GoldenQuery`` — frozen dataclass для retrieval test query (Phase 5).
    - ``load_golden_facts`` — JSONL loader.
    - ``load_golden_queries`` — JSONL loader (Phase 5).
    - ``load_session_messages`` — JSONL loader для OpenAI-shape session.
    - ``fact_id_to_relevant_memory_id`` — turn_index-based mapping (Phase 5).
    - ``session_to_corpus`` — session → ``Memory`` corpus (Phase 5).
    - ``ContextRetentionMetric`` — B1 metric (recall на marked facts через BM25).
    - ``PrecisionMetric`` — B2 metric (precision@5 на golden queries, Phase 5).
    - ``RecallMetric`` — B3 metric (recall@20 на golden queries, Phase 5).
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
    GoldenQuery,
    fact_id_to_relevant_memory_id,
    load_golden_facts,
    load_golden_queries,
    load_session_messages,
)
from harness.eval.retention import (
    ContextRetentionMetric,
    RetentionResult,
)
from harness.eval.retrieval import (
    PrecisionMetric,
    PrecisionResult,
    RecallMetric,
    RecallResult,
    session_to_corpus,
)
from harness.eval.filler import (
    FillerDetector,
    FillerDetectorConfig,
)
from harness.eval.reranker import (
    LengthNormalizedReranker,
    RerankerConfig,
)
from harness.eval.compaction_loss import (
    CompactionLossMetric,
    LossResult,
)
from harness.eval.runner import EvalRunner

__all__ = [
    "GoldenFact",
    "GoldenQuery",
    "fact_id_to_relevant_memory_id",
    "load_golden_facts",
    "load_golden_queries",
    "load_session_messages",
    "session_to_corpus",
    "ContextRetentionMetric",
    "RetentionResult",
    "PrecisionMetric",
    "PrecisionResult",
    "RecallMetric",
    "RecallResult",
    "FillerDetector",
    "FillerDetectorConfig",
    "LengthNormalizedReranker",
    "RerankerConfig",
    "CompactionLossMetric",
    "LossResult",
    "EvalRunner",
]
