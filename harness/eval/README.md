# harness/eval/ — Phase 3 B-mini + Phase 5 B2/B3 metrics

Phase 3 metrics implementation (B1 context retention, B4 compaction loss)
plus the Phase 5 retrieval metrics (B2 precision@5, B3 recall@20).
B5 (tool-use success rate) is deferred to Phase 5.2.

## Quick start

```python
from harness.eval import (
    GoldenFact,
    GoldenQuery,
    ContextRetentionMetric,
    CompactionLossMetric,
    PrecisionMetric,
    RecallMetric,
    EvalRunner,
    session_to_corpus,
)

# B1 — context retention (sync)
retention = ContextRetentionMetric(top_k=20)
result = retention.measure(session_messages, golden_facts)
assert result.ratio >= 0.95

# B2 — precision@5 (sync)
corpus = session_to_corpus(session_messages)
precision = PrecisionMetric(k=5)
result = precision.measure(corpus, golden_queries, golden_facts)
assert result.threshold_ratio >= 0.7  # B2 DoD (Phase 5)

# B3 — recall@20 (sync)
recall = RecallMetric(k=20)
result = recall.measure(corpus, golden_queries, golden_facts)
assert result.threshold_ratio >= 0.85  # B3 DoD (Phase 5)

# B4 — compaction loss (async)
loss = CompactionLossMetric()
result = await loss.measure(session_messages, golden_facts, compactor, "qwen3:8b")
assert result.ratio >= 0.95

# EvalRunner — async orchestrator
runner = EvalRunner()
pr = await runner.run_precision(corpus, queries, facts, k=5)
rr = await runner.run_recall(corpus, queries, facts, k=20)
```

## Trust boundary

`harness/eval/` **must not** import from `harness/agents/` or
`harness/server/`. Only `harness.memory.retrieval.bm25`,
`harness.memory.schema`, `harness.config`, `harness.context`, and stdlib.

Verified by `tests/eval/test_eval_trust_boundary.py` (parametrized
over all `.py` files in this package, mirror of the
`test_runner_does_not_import_v150.py` pattern).

## Adding a new metric

1. Create `harness/eval/<metric_name>.py` with a frozen dataclass
   result and a `measure()` method (sync or async).
2. Add a re-export to `harness/eval/__init__.py`.
3. Add a `run_<metric_name>()` method to `EvalRunner`.
4. Write a golden test in `tests/eval/test_<metric_name>_golden.py`.
5. Update `tests/eval/conftest.py` with any new fixtures.

## Conventions

- **Golden facts** are 50 (n=50, uniform distribution: 12 early / 26 mid
  / 12 late). Phrases are specific (e.g. "Qdrant primary", "Phase 3 v1.5.0")
  so BM25 can lift them above generic words.
- **Golden queries** are 50 (Phase 5: 30 auto + 20 manual). 45 contribute
  to the B2/B3 threshold (factual_lookup + paraphrased); 5 multi_hop
  are reported in `per_category` but excluded from the main DoD
  (per `docs/PHASE5-B2-B3-PLAN.md` sign-off 2026-06-16).
- **Substring match** is case-insensitive. R2/C4 limitations:
  - "v1.5.0" matches "we did not preserve v1.5.0" (forgiving).
  - Semantic paraphrases ("Qdrant is the primary store" vs "Qdrant primary")
    are NOT matched — future Phase 5.1 dense retriever work.
- **Mock LLM** for B4: the compactor is constructed with a
  `LLMRouter` subclass (see `tests/eval/conftest.py:mock_summariser`)
  — `AsyncMock` does not unwrap correctly in the compactor's
  `await self._router.completion(...)` path. The mock summariser
  inserts all phrases into the summary, so the contract test
  (`test_b4_loss_mock_summariser_preserves_facts`) passes with
  ratio = 1.0.

## Pilot results (16.06.2026, BM25 sparse on seed_session_100)

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| B2 precision@5 | >= 0.7 | 0.191 | NOT MET — BM25 sparse insufficient (assistant turns dominate top-5) |
| B3 recall@20 | >= 0.85 | 0.843 | NOT MET (0.7pp below) |

B2/B3 strict thresholds are deferred to **Phase 5.1** (hybrid
retriever with dense + sparse, see `docs/PHASE5-B2-B3-PLAN.md`).
Phase 5.0 ships the **infrastructure** (50-query dataset, metric
classes, runner integration, per_category breakdowns) — the
thresholds are reported but not enforced as gating.

## Known limitations (deferred to Phase 5.1+)

- C3: `IdentityReranker` — purely lexical, no semantic preservation.
- C4: substring match is forgiving (case-insensitive, no position check).
- C5: `_Summariser` Protocol is structural (runtime_checkable=False).
  Bad mocks fail at call time, not at injection time.
- B2/B3: BM25 sparse insufficient for precision@5 (assistant turns
  with "ack and continue" filler dominate top-5). Phase 5.1 = hybrid
  retriever (DenseRetriever + HybridRetriever with RRF k=60).
- B5: tool-use success rate T1/T2/T3 — requires LLM provider keys
  and structured output, deferred to Phase 5.2.
- C11: `compaction_threshold_ratio > compaction_target_ratio`
  validator not documented in this README (see `harness/config.py:1132`).
- R5 (closed 16.06.2026): `compaction.py:712-739` `force_compact`
  marker mismatch was fixed. Legacy `[Conversation summary]` form
  is still accepted for back-compat with pre-v1.4.0 cached summaries.
  Regression test: `tests/eval/test_force_compact_regression.py`.
