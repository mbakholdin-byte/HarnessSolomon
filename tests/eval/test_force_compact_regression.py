"""Phase 3 v1.5.x — R5 regression: force_compact summary_preview extraction.

The pre-fix ``ContextCompactor.force_compact`` (compaction.py:712-725)
hard-coded two values that did NOT match what ``_inject_summary``
emits:

    - marker: code searched for ``"[Conversation summary]"``,
      but ``_inject_summary`` (line 891) emits
      ``"[Compaction summary — earlier turns condensed]"``.
    - role: code filtered for ``role="system"``, but
      ``_inject_summary`` emits ``role="user"``.

Result: ``CompactResult.summary_preview`` was always
``"(no summary generated)"`` even when the slow path (sliding window
+ LLM summary) actually ran. This is a pre-existing production bug
that B-mini worked around by testing ``maybe_compact`` directly
(test_compaction_loss_golden.py:46). The fix in
compaction.py:712-739 now matches the real marker/role and accepts
the legacy ``[Conversation summary]`` form for back-compat with
pre-v1.4.0 cached summaries.

Test scope (3 tests):
    - test_force_compact_summary_preview_not_placeholder: regression
      guard — preview is the injected summary, NOT the placeholder.
    - test_force_compact_summary_preview_contains_marker: the
      preview starts with the real marker (or contains it).
    - test_force_compact_summary_preview_truncates_at_200: 200-char
      cap on preview is preserved.
"""
from __future__ import annotations

import pytest

from harness.context import CompactResult, ContextCompactor


pytestmark = pytest.mark.asyncio


async def test_force_compact_summary_preview_not_placeholder(
    seed_session_100: list[dict],
    compactor: ContextCompactor,
) -> None:
    """R5 regression: ``summary_preview`` is no longer "(no summary generated)".

    Pre-fix: compactor would always return the placeholder because
    the marker/role filter never matched the real injected message.
    Post-fix: compactor slices the body of the real ``role="user"``
    summary message up to the first ``\\n\\n``.
    """
    result = await compactor.force_compact(seed_session_100, "qwen3:8b")

    assert isinstance(result, CompactResult)
    assert result.summary_preview != "(no summary generated)", (
        "R5 NOT fixed: force_compact still returns placeholder for "
        "summary_preview; check compaction.py:712-739 marker/role filter"
    )
    assert result.summary_preview, "summary_preview is empty"
    # Mock contract: the conftest mock_summariser injects all 50 phrases.
    # The preview should contain the marker line and the first few
    # preserved-fact bullets.
    assert "[Compaction summary" in result.summary_preview, (
        f"preview does not start with the real marker; got: "
        f"{result.summary_preview[:120]!r}"
    )


async def test_force_compact_summary_preview_contains_marker(
    seed_session_100: list[dict],
    compactor: ContextCompactor,
) -> None:
    """R5 regression: preview contains the real ``[Compaction summary`` marker.

    Compactor slices the body after the marker up to the first
    ``\\n\\n`` separator (preserved by ``_inject_summary``). So the
    preview MUST include the marker itself.
    """
    result = await compactor.force_compact(seed_session_100, "qwen3:8b")

    assert "[Compaction summary" in result.summary_preview, (
        f"marker missing from preview; got: {result.summary_preview!r}"
    )
    # And the preview should be a *prefix* of the injected message
    # (not the whole thing) — because the compactor splits on ``\n\n``.
    # Mock body is short, so the preview is the first paragraph only.
    assert len(result.summary_preview) <= 200, (
        f"preview exceeds 200-char cap: {len(result.summary_preview)} chars"
    )


async def test_force_compact_summary_preview_truncates_at_200() -> None:
    """The 200-char cap on summary_preview is preserved by the fix.

    We construct a compactor whose mock summary is a long string
    (>200 chars) and verify the preview is sliced to ≤200 chars
    (or ≤201 with the ``…`` suffix).
    """
    from unittest.mock import AsyncMock
    from harness.config import Settings
    from harness.server.llm.router import LLMRouter, CompletionResult

    long_body = "X" * 500  # 500-char summary

    class _LongFakeRouter(LLMRouter):
        async def _call_litellm_completion(self, model, messages, **kwargs):
            return None

        def _normalize_completion(self, model, response):
            body = (
                "[Compaction summary — earlier turns condensed]\n\n"
                f"{long_body}\n\nMore stuff"
            )
            return CompletionResult(content=body, model=model, usage={})

    settings = Settings(
        compaction_enabled=True,
        compaction_threshold_ratio=0.005,
        compaction_target_ratio=0.001,
        compaction_keep_recent_turns=4,
        compaction_summarizer_max_input_tokens=4000,
        compaction_persist_to_memory=False,
    )
    c = ContextCompactor(
        settings,
        _LongFakeRouter.__new__(_LongFakeRouter),  # type: ignore[arg-type]
        memory=None,
        session_id="r5-cap-test",
        store=None,
        audit=None,
        pre_compact_hook=None,
        idle_trigger=None,
    )

    # Build a session that crosses the threshold.
    session = [
        {"role": "system", "content": "You are a helper."},
    ]
    for i in range(120):
        session.append({"role": "user", "content": f"Q{i} " * 200})
        session.append({"role": "assistant", "content": f"A{i} " * 200})

    result = await c.force_compact(session, "qwen3:8b")

    # The cap is 200 chars + optional "…" suffix.
    assert len(result.summary_preview) <= 201, (
        f"preview exceeds 200-char cap: {len(result.summary_preview)} chars; "
        f"got: {result.summary_preview!r}"
    )
    assert "[Compaction summary" in result.summary_preview
