"""Phase 3 B-mini: B4 - compaction loss golden test.

Measures how many marked ``GoldenFact`` instances are preserved in the
summary message produced by ``ContextCompactor.maybe_compact``. Target:
ratio >= 0.95 (< 5% loss).

Why ``maybe_compact`` (not ``force_compact``):
    - ``maybe_compact`` returns the compacted message list (not a
      ``CompactResult``), so we can extract the summary message via
      the B1+B5 fix matcher. The metric accepts both
      ``[Compaction summary`` and ``[Conversation summary]`` markers
      so it also works on ``force_compact`` output (see
      ``tests/eval/test_force_compact_regression.py`` for the
      dedicated R5 regression test).

Test scope (5 tests):
    - test_b4_loss_below_5pct: 50 facts -> ratio = 1.0
      (mock contract: all phrases in summary).
    - test_b4_loss_mock_summariser_preserves_facts: explicit mock
      contract test - all 50 phrases in summary content.
    - test_b4_loss_no_compact_needed: short session under threshold
      -> fallback to trimmed list (B7).
    - test_b4_loss_disabled_compaction: ``compaction_enabled=False``
      -> fallback path (B7).
    - test_b4_loss_summary_message_role: summary has role="user"
      (B5 fix regression guard).
"""
from __future__ import annotations

import pytest

from harness.config import Settings
from harness.context import ContextCompactor
from harness.eval import CompactionLossMetric, GoldenFact
from unittest.mock import AsyncMock
from harness.server.llm.router import CompletionResult
from harness.eval.compaction_loss import _extract_summary


pytestmark = pytest.mark.asyncio


async def test_b4_loss_below_5pct(
    seed_session_100: list[dict],
    golden_facts: list[GoldenFact],
    compactor: ContextCompactor,
) -> None:
    """B4 DoD: ratio >= 0.95 on 50 marked facts.

    Uses ``maybe_compact`` + extract summary via ``_extract_summary``
    (handles both ``[Compaction summary`` and ``[Conversation summary]``
    markers). Mock summariser injects all 50 phrases -> ratio = 1.0.
    """
    metric = CompactionLossMetric()
    result = await metric.measure(seed_session_100, golden_facts, compactor, "qwen3:8b")

    assert result.total == 50
    assert result.preserved >= 48, (
        f"compaction loss too high: preserved={result.preserved}/50, "
        f"missing={[f.id for f in result.missing]}"
    )
    assert result.ratio >= 0.95
    assert result.summary_text is not None
    assert not result.fallback_used
    # Mock contract: all 50 phrases should be in the summary.
    assert result.preserved == 50, (
        f"mock contract broken: only {result.preserved}/50 phrases in summary"
    )


async def test_b4_loss_mock_summariser_preserves_facts(
    seed_session_100: list[dict],
    golden_facts: list[GoldenFact],
    compactor: ContextCompactor,
) -> None:
    """Mock contract: all 50 fact phrases appear in the summary content."""
    metric = CompactionLossMetric()
    result = await metric.measure(seed_session_100, golden_facts, compactor, "qwen3:8b")

    assert result.summary_text is not None
    summary_lower = result.summary_text.lower()
    for f in golden_facts:
        assert f.phrase.lower() in summary_lower, (
            f"mock contract: phrase '{f.phrase}' missing from summary"
        )


async def test_b4_loss_no_compact_needed(
    golden_facts: list[GoldenFact],
    eval_settings: Settings,
    mock_summariser: AsyncMock,
) -> None:
    """Short session under threshold -> fallback to trimmed list (B7)."""
    short_session = [
        {"role": "system", "content": "You are a helper."},
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "Hi! Phase 3 v1.5.0 is closed."},
    ]
    short_fact = GoldenFact(
        id="F_SHORT", phrase="Phase 3 v1.5.0",
        turn_index=0, category="user",
    )
    settings = Settings(
        compaction_enabled=True,
        compaction_threshold_ratio=0.99,
        compaction_target_ratio=0.5,
        compaction_persist_to_memory=False,
    )
    c = ContextCompactor(
        settings, mock_summariser,  # type: ignore[arg-type]
        memory=None, session_id="b4-short",
        store=None, audit=None,
        pre_compact_hook=None, idle_trigger=None,
    )
    metric = CompactionLossMetric()
    result = await metric.measure(short_session, [short_fact], c, "qwen3:8b")

    assert result.total == 1
    assert result.preserved == 1
    assert result.ratio == 1.0
    assert result.fallback_used
    assert result.summary_text is None


async def test_b4_loss_disabled_compaction(
    seed_session_100: list[dict],
    golden_facts: list[GoldenFact],
    eval_settings: Settings,
    mock_summariser: AsyncMock,
) -> None:
    """``compaction_enabled=False`` -> no summary, fallback path.

    All 50 facts remain in the trimmed list verbatim -> ratio = 1.0
    via fallback.
    """
    settings = Settings(
        compaction_enabled=False,
        compaction_persist_to_memory=False,
    )
    c = ContextCompactor(
        settings, mock_summariser,  # type: ignore[arg-type]
        memory=None, session_id="b4-disabled",
        store=None, audit=None,
        pre_compact_hook=None, idle_trigger=None,
    )
    metric = CompactionLossMetric()
    result = await metric.measure(seed_session_100, golden_facts, c, "qwen3:8b")

    assert result.preserved == 50
    assert result.ratio == 1.0
    assert result.fallback_used
    assert result.summary_text is None


async def test_b4_loss_summary_message_role(
    seed_session_100: list[dict],
    compactor: ContextCompactor,
) -> None:
    """Summary message has ``role=\"user\"``, NOT ``role=\"system\"`` (B5 fix).

    Regression guard: the compactor's ``_inject_summary`` emits a
    user-role message. If a future refactor accidentally changes this
    to ``system``, the metric's extractor would still find it (we match
    on marker), but downstream callers expecting user-role would break.
    """
    compacted = await compactor.maybe_compact(seed_session_100, "qwen3:8b")
    summary_msg = next(
        (m for m in compacted
         if m.get("role") == "user"
         and "[Compaction summary" in m.get("content", "")),
        None,
    )
    assert summary_msg is not None, (
        "no user-role summary message found in compacted list; "
        "compactor may have skipped the LLM path"
    )
    assert summary_msg.get("role") == "user"
    extracted = _extract_summary(compacted)
    assert extracted == summary_msg["content"]
