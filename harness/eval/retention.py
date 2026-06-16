"""Phase 3 B-mini: B1 — Context retention metric.

``ContextRetentionMetric`` measures how many marked facts (``GoldenFact``)
are retrievable from a session after compaction (or without, as baseline).

**Algorithm:**
  1. Convert session messages (OpenAI dicts) into a ``Memory`` corpus
     (B3 fix: ``BM25Retriever`` requires ``Memory``, not dicts).
  2. Build a ``BM25Retriever`` over the corpus (NOT ``RetrievalPipeline``
     — that returns an assembled string, B2 fix).
  3. For each ``GoldenFact``, run ``retriever.retrieve(fact.phrase, k=20)``
     (R3 fix: k=20, not k=5, to compensate for rare-fact sparsity).
  4. A fact is **retained** if its phrase (case-insensitive substring)
     appears in any of the top-20 retrieved ``Memory.content`` fields.
  5. ``RetentionResult.top_doc_ids`` records the per-fact Memory ids
     (R2 fix: stronger assertion than "phrase anywhere in context").

**Trust boundary:** импорт ``harness.memory.retrieval.bm25.BM25Retriever``
(Protocol-only), ``harness.memory.schema.Memory``, ``harness.eval.golden``.
НЕ импортирует ``harness.agents``, ``harness.server``, ``harness.context``,
or any LLM router. Test-only, no production data access.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from harness.eval.golden import GoldenFact
from harness.memory.retrieval.bm25 import BM25Retriever
from harness.memory.schema import Memory


@dataclass(frozen=True)
class RetentionResult:
    """Outcome of one ``ContextRetentionMetric.measure`` call.

    Attributes:
        total: Total number of golden facts measured.
        retained: Number of facts whose phrase was found in top-k results.
        ratio: ``retained / total`` (0.0 if total == 0).
        missing: List of ``GoldenFact`` instances that were NOT retained.
            Useful for debugging: which facts are lost after compaction.
        top_doc_ids: ``{fact_id: [memory_id, ...]}`` for every fact.
            Top-20 Memory ids that matched the fact's phrase (R2 fix:
            enables per-fact assertion, future B2/B3 integration).
    """

    total: int
    retained: int
    ratio: float
    missing: list[GoldenFact] = field(default_factory=list)
    top_doc_ids: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # ratio is derivable but convenient — verify consistency.
        if self.total > 0 and abs(self.ratio - self.retained / self.total) > 1e-9:
            raise ValueError(
                f"ratio {self.ratio} != retained/total "
                f"({self.retained}/{self.total})"
            )


class ContextRetentionMetric:
    """B1 — measure how many marked facts survive in a session.

    Usage::

        metric = ContextRetentionMetric(top_k=20)
        corpus = [Memory(id=f"m{i}", content=json.dumps(m, ensure_ascii=False),
                         layer="L2", source="session")
                  for i, m in enumerate(session_messages)]
        result = metric.measure(session_messages, facts)
        assert result.ratio >= 0.95
    """

    def __init__(self, top_k: int = 20) -> None:
        if top_k <= 0:
            raise ValueError(f"top_k must be > 0, got {top_k}")
        self._top_k = top_k

    def measure(
        self,
        session: list[dict],
        facts: list[GoldenFact],
    ) -> RetentionResult:
        """Measure retention of ``facts`` in ``session``.

        Args:
            session: OpenAI-shape chat history (list of dicts with
                ``role`` and ``content``). Empty list returns a zero
                ratio with no missing (all 50 facts "missing" if list
                empty, see ``test_b1_empty_corpus_returns_zero``).
            facts: Marked facts to check.

        Returns:
            ``RetentionResult`` with retention ratio and per-fact details.
        """
        if not facts:
            return RetentionResult(total=0, retained=0, ratio=1.0)
        # B3 fix: convert session dicts to Memory corpus.
        corpus = [
            Memory(
                id=f"m{i}",
                content=json.dumps(msg, ensure_ascii=False),
                layer="L2",
                source="manual",
            )
            for i, msg in enumerate(session)
        ]
        retriever = BM25Retriever(corpus)
        retained = 0
        missing: list[GoldenFact] = []
        top_doc_ids: dict[str, list[str]] = {}
        for fact in facts:
            # B7 / R2 fix: case-insensitive substring match in top-k
            # Memory.content, AND record the matched Memory ids.
            matches = retriever.retrieve(fact.phrase, k=self._top_k)
            matched_ids = [
                mem.id for mem, _score in matches
                if fact.phrase.lower() in mem.content.lower()
            ]
            top_doc_ids[fact.id] = [mem.id for mem, _score in matches]
            if matched_ids:
                retained += 1
            else:
                missing.append(fact)
        return RetentionResult(
            total=len(facts),
            retained=retained,
            ratio=retained / len(facts),
            missing=missing,
            top_doc_ids=top_doc_ids,
        )


__all__ = ["ContextRetentionMetric", "RetentionResult"]
