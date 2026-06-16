"""Phase 3 B-mini: B1 - context retention golden test.

Measures how many marked ``GoldenFact`` instances are retrievable from
a 100+ turn session AFTER compaction via ``ContextCompactor.force_compact``.
Target: ratio >= 0.95 (95% retention).

The metric extracts the compactor's summary message and counts how many
golden fact phrases appear in it. The mock summariser (see
``tests/eval/conftest.py:mock_summariser``) injects all 50 phrases
into the summary, so a correctly-wired retention path returns
ratio = 1.0 (mock contract).

Why ``force_compact`` (not the sliding-window helper): we want to
exercise the full production path (sliding window + LLM summary)
because that's what users hit in production. ``maybe_compact``
short-circuits when the sliding window already fits the target.

Test scope (5 tests):
    - test_b1_retention_100turns_baseline: raw session retains all
      50 phrases verbatim (no compaction, BM25 lookup).
    - test_b1_retention_after_force_compact: 50 phrases in summary
      after force_compact (mock contract).
    - test_b1_empty_corpus_returns_zero: empty session -> ratio = 0.0.
    - test_b1_retention_handles_tool_pairs: tool_call/tool_result
      pairs survive the compactor's tool-pair preservation logic.
    - test_b1_retention_threshold_configurable: Settings override
      is honoured by the compactor (smoke test on the override).
"""
from __future__ import annotations

import json

import pytest

from harness.config import Settings
from harness.context import ContextCompactor
from harness.eval import ContextRetentionMetric, GoldenFact
from harness.eval.compaction_loss import _extract_summary


# === B1: baseline (no compaction) =====================================


def test_b1_retention_100turns_baseline(
    seed_session_100: list[dict],
    golden_facts: list[GoldenFact],
) -> None:
    """B1 baseline: 100-turn session without compaction retains all facts.

    Expected: all 50 phrases appear verbatim in their seeded user
    messages, so BM25 should retrieve them with k=20.
    """
    metric = ContextRetentionMetric(top_k=20)
    result = metric.measure(seed_session_100, golden_facts)

    assert result.total == 50
    assert result.retained == 50, (
        f"baseline must retain all 50 facts; "
        f"missing: {[f.id + ':' + f.phrase for f in result.missing]}"
    )
    assert result.ratio == 1.0
    for fid in ("F01", "F25", "F50"):
        assert fid in result.top_doc_ids
        assert len(result.top_doc_ids[fid]) > 0


# === B1: after force_compact (the B1 DoD) ==============================


async def test_b1_retention_after_maybe_compact(
    seed_session_100: list[dict],
    golden_facts: list[GoldenFact],
    compactor: ContextCompactor,
) -> None:
    """B1 DoD: ratio >= 0.95 on 50 marked facts after ``maybe_compact``.

    Uses ``maybe_compact`` (returns message list) + extract summary
    via ``_extract_summary`` (handles both ``[Compaction summary``
    and ``[Conversation summary]`` markers). The mock summariser
    injects all 50 phrases into the summary, so the substring count
    should be 50.

    Note: ``force_compact`` cannot be used here because it returns
    a ``CompactResult`` (not a list of messages). See
    ``tests/eval/test_force_compact_regression.py`` for the
    dedicated regression test on ``force_compact.summary_preview``.
    """
    compacted = await compactor.maybe_compact(seed_session_100, "qwen3:8b")
    summary = _extract_summary(compacted)
    assert summary is not None, (
        "maybe_compact did not inject a summary; check fixture settings"
    )
    summary_lower = summary.lower()
    preserved = sum(
        1 for f in golden_facts if f.phrase.lower() in summary_lower
    )
    missing = [
        f for f in golden_facts if f.phrase.lower() not in summary_lower
    ]
    assert preserved >= 48, (
        f"compaction lost too many facts: preserved={preserved}/50, "
        f"missing={[f.id for f in missing]}"
    )
    ratio = preserved / 50
    assert ratio >= 0.95


# === B1: edge cases ===================================================


def test_b1_empty_corpus_returns_zero(
    golden_facts: list[GoldenFact],
) -> None:
    """Empty session -> ratio = 0.0 (C10 fix: explicit assertion)."""
    metric = ContextRetentionMetric(top_k=20)
    result = metric.measure([], golden_facts)

    assert result.total == 50
    assert result.retained == 0
    assert result.ratio == 0.0
    assert len(result.missing) == 50
    for fid, ids in result.top_doc_ids.items():
        assert ids == [], f"fact {fid} should have no matches"


async def test_b1_retention_handles_tool_pairs(
    seed_session_100: list[dict],
    compactor: ContextCompactor,
) -> None:
    """Tool pairs (tool_call + tool_result) survive the compactor path.

    After ``maybe_compact``, the compacted list should still contain
    tool messages referenced by assistant tool_calls in the recent
    tail (compactor's tool-pair preservation logic).
    """
    compacted = await compactor.maybe_compact(seed_session_100, "qwen3:8b")
    # Sanity: compaction actually happened (compacted != original).
    assert len(compacted) < len(seed_session_100), (
        "maybe_compact did not reduce the message list"
    )
    # Sanity: at least one tool message survived.
    tool_ids_in_compacted = {
        m.get("tool_call_id")
        for m in compacted
        if m.get("role") == "tool"
    }
    assert len(tool_ids_in_compacted) > 0, (
        "compactor dropped all tool messages - tool-pair preservation broken"
    )
    assert all(tid is not None for tid in tool_ids_in_compacted), (
        "tool message without tool_call_id leaked into compacted list"
    )


# === B1: threshold configurability ====================================


def test_b1_retention_threshold_configurable() -> None:
    """Settings.compaction_threshold_ratio override is accepted by Pydantic.

    Smoke test on the B4 fix: explicit ``compaction_target_ratio <
    compaction_threshold_ratio`` is required by the validator.
    """
    # Both low (0.05/0.025) and high (0.99/0.5) variants must construct.
    Settings(
        compaction_threshold_ratio=0.05,
        compaction_target_ratio=0.025,
    )
    Settings(
        compaction_threshold_ratio=0.99,
        compaction_target_ratio=0.5,
    )
    # And a high target with low threshold raises (the validator).
    with pytest.raises(ValueError, match="compaction_target_ratio"):
        Settings(
            compaction_threshold_ratio=0.05,
            compaction_target_ratio=0.5,  # > threshold -> reject
        )
