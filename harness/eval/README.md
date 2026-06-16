# harness/eval/ — Phase 3 B-mini metrics

Phase 3 metrics implementation (context retention + compaction loss) plus
the eval-harness scaffold for Phase 5 (B2 precision@5, B3 recall@20,
B5 tool-use success rate).

## Quick start

```python
from harness.eval import (
    GoldenFact,
    ContextRetentionMetric,
    CompactionLossMetric,
    EvalRunner,
)

# B1 — context retention (sync)
retention = ContextRetentionMetric(top_k=20)
result = retention.measure(session_messages, golden_facts)
assert result.ratio >= 0.95

# B4 — compaction loss (async)
loss = CompactionLossMetric()
result = await loss.measure(session_messages, golden_facts, compactor, "qwen3:8b")
assert result.ratio >= 0.95
```

## Trust boundary

`harness/eval/` **must not** import from `harness.agents/` or
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
- **Substring match** is case-insensitive. R2/C4 limitations:
  - "v1.5.0" matches "we did not preserve v1.5.0" (forgiving).
  - Semantic paraphrases ("Qdrant is the primary store" vs "Qdrant primary")
    are NOT matched — future B2 work.
- **Mock LLM** for B4: the compactor is constructed with `AsyncMock`
  per `tests/test_context_compaction.py:205-209` pattern. The mock
  summariser inserts all phrases into the summary, so the contract
  test (`test_b4_loss_mock_summariser_preserves_facts`) passes with
  ratio = 1.0.

## Known limitations (deferred to Phase 5)

- C3: `IdentityReranker` — purely lexical, no semantic preservation.
- C4: substring match is forgiving (case-insensitive, no position check).
- C5: `_Summariser` Protocol is structural (runtime_checkable=False).
  Bad mocks fail at call time, not at injection time.
- B2/B3/B5 (precision@5, recall@20, tool-use T1/T2/T3) require
  golden datasets with manual labels and LLM provider keys.
- C11: `compaction_threshold_ratio > compaction_target_ratio`
  validator not documented in this README (see `harness/config.py:1132`).
- R5: `compaction.py:715` `force_compact` has a marker bug
  ("[Conversation summary]" instead of "[Compaction summary — earlier
  turns condensed]"). B-mini uses `maybe_compact` only.
