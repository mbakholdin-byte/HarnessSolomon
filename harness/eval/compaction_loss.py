"""Phase 3 B-mini: B4 — Compaction loss metric.

``CompactionLossMetric`` measures how many marked facts (``GoldenFact``)
survive in the summary message produced by ``ContextCompactor.maybe_compact``.

**Algorithm:**
  1. Run ``await compactor.maybe_compact(session, model_name)`` (NOT
     ``force_compact`` — R5 fix: that has a different marker).
  2. Extract the summary message (B1 + B5 fix):
     - ``role == "user"`` (NOT "system" — compactor produces user-role)
     - ``"[Compaction summary" in content`` (NOT ``startswith`` — actual
       marker is ``"[Compaction summary — earlier turns condensed]"``)
  3. **Fallback** (B7 fix): if no summary message exists (e.g. session
     under threshold, or ``compaction_enabled=False``), check facts in
     the **trimmed list** — substring match in any message content.
  4. For each ``GoldenFact``, check ``fact.phrase.lower() in summary.lower()``.
  5. ``ratio = preserved / total``.

**Trust boundary:** импорт ``harness.context.ContextCompactor``,
``harness.eval.golden``. The compactor is constructed with
``store=None, memory=None, audit=None, pre_compact_hook=None,
idle_trigger=None`` (B6 fix) — no real L2/DB writes, no audit I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from harness.eval.golden import GoldenFact


# Markers emitted by ``ContextCompactor._inject_summary``.
# ``_inject_summary`` (compaction.py:891) emits a ``role="user"``
# message with ``"[Compaction summary — earlier turns condensed]"``
# (em-dash). The legacy ``force_compact`` preview path (compaction.py:712)
# also recognises ``[Conversation summary]`` for back-compat with
# pre-v1.4.0 cached summaries. We accept either marker so the metric
# works on both ``maybe_compact`` and ``force_compact`` outputs.
_SUMMARY_MARKERS = ("[Compaction summary", "[Conversation summary]")


@dataclass(frozen=True)
class LossResult:
    """Outcome of one ``CompactionLossMetric.measure`` call.

    Attributes:
        total: Total number of golden facts measured.
        preserved: Number of facts whose phrase was found in the
            summary (or, as fallback, in the trimmed message list).
        ratio: ``preserved / total`` (0.0 if total == 0).
        missing: List of ``GoldenFact`` instances that were NOT preserved.
        summary_text: The extracted summary message content, or
            ``None`` if no summary was produced (fallback was used).
        fallback_used: True if no summary message was found and the
            metric fell back to checking the trimmed list.
    """

    total: int
    preserved: int
    ratio: float
    missing: list[GoldenFact] = field(default_factory=list)
    summary_text: str | None = None
    fallback_used: bool = False

    def __post_init__(self) -> None:
        if self.total > 0 and abs(self.ratio - self.preserved / self.total) > 1e-9:
            raise ValueError(
                f"ratio {self.ratio} != preserved/total "
                f"({self.preserved}/{self.total})"
            )


def _extract_summary(messages: list[dict]) -> str | None:
    """Find the compactor's summary message in a compacted message list.

    Accepts both ``[Compaction summary`` (em-dash, em-dash — emitted
    by ``_inject_summary`` for both ``maybe_compact`` and
    ``force_compact``) and the legacy ``[Conversation summary]``
    marker. Returns the content string of the first match, or
    ``None`` if no summary is present.
    """
    for m in messages:
        content = m.get("content", "") or ""
        if any(marker in content for marker in _SUMMARY_MARKERS):
            return content
    return None


def _fact_in_messages(fact: GoldenFact, messages: list[dict]) -> bool:
    """Check if a fact's phrase appears in any message content.

    Used as the no-summary fallback (B7 fix).
    """
    phrase = fact.phrase.lower()
    return any(phrase in m.get("content", "").lower() for m in messages)


class CompactionLossMetric:
    """B4 — measure how many marked facts survive in the compactor summary.

    Usage::

        metric = CompactionLossMetric()
        result = await metric.measure(session, facts, compactor, "qwen3:8b")
        assert result.ratio >= 0.95
    """

    async def measure(
        self,
        session: list[dict],
        facts: list[GoldenFact],
        compactor: "ContextCompactor",  # noqa: F821 — forward ref, no import
        model_name: str,
    ) -> LossResult:
        """Run ``compactor.maybe_compact`` and check fact preservation.

        Args:
            session: OpenAI-shape chat history.
            facts: Marked facts to check in the summary.
            compactor: A ``ContextCompactor`` instance. The metric does
                NOT construct it — that's the test's responsibility
                (B6 fix: keep all test isolation in conftest).
            model_name: Model id passed to ``maybe_compact`` for the
                context-window lookup.

        Returns:
            ``LossResult`` with preservation ratio, missing facts,
            and a flag for whether the no-summary fallback was used.
        """
        if not facts:
            return LossResult(total=0, preserved=0, ratio=1.0)
        # Run the compactor. We use ``maybe_compact`` (returns a list of
        # messages) rather than ``force_compact`` (returns a
        # ``CompactResult``) so we can extract the summary message
        # from the list directly.
        compacted = await compactor.maybe_compact(session, model_name)
        summary = _extract_summary(compacted)
        if summary is None:
            # B7 fix: no summary → fallback to trimmed list.
            preserved = sum(
                1 for f in facts if _fact_in_messages(f, compacted)
            )
            missing = [f for f in facts if not _fact_in_messages(f, compacted)]
            return LossResult(
                total=len(facts),
                preserved=preserved,
                ratio=preserved / len(facts),
                missing=missing,
                summary_text=None,
                fallback_used=True,
            )
        # Normal path: check facts in summary.
        summary_lower = summary.lower()
        preserved = sum(
            1 for f in facts if f.phrase.lower() in summary_lower
        )
        missing = [
            f for f in facts if f.phrase.lower() not in summary_lower
        ]
        return LossResult(
            total=len(facts),
            preserved=preserved,
            ratio=preserved / len(facts),
            missing=missing,
            summary_text=summary,
            fallback_used=False,
        )

    async def measure_force(
        self,
        session: list[dict],
        facts: list[GoldenFact],
        compactor: "ContextCompactor",  # noqa: F821 — forward ref, no import
        model_name: str,
    ) -> LossResult:
        """Run ``compactor.force_compact`` (manual /compact path).

        ``force_compact`` ALWAYS runs the slow path (sliding window +
        LLM + summary injection), unlike ``maybe_compact`` which
        short-circuits if the sliding window already fits the target.
        B4 golden tests use this entry point so the summary message is
        always produced and the LLM mock contract is exercised.

        ``force_compact`` uses the SAME ``[Compaction summary``
        marker (via the shared ``_inject_summary``) as
        ``maybe_compact``, so the substring extractor works on both
        paths. The dedicated R5 regression test in
        ``tests/eval/test_force_compact_regression.py`` covers the
        ``force_compact.summary_preview`` field on the returned
        ``CompactResult``.
        """
        if not facts:
            return LossResult(total=0, preserved=0, ratio=1.0)
        compacted = await compactor.force_compact(session, model_name)
        summary = _extract_summary(compacted)
        if summary is None:
            # Defensive: force_compact should always produce a summary.
            # If it doesn't (e.g. summariser returned empty), fall back.
            preserved = sum(
                1 for f in facts if _fact_in_messages(f, compacted)
            )
            missing = [f for f in facts if not _fact_in_messages(f, compacted)]
            return LossResult(
                total=len(facts),
                preserved=preserved,
                ratio=preserved / len(facts),
                missing=missing,
                summary_text=None,
                fallback_used=True,
            )
        summary_lower = summary.lower()
        preserved = sum(
            1 for f in facts if f.phrase.lower() in summary_lower
        )
        missing = [
            f for f in facts if f.phrase.lower() not in summary_lower
        ]
        return LossResult(
            total=len(facts),
            preserved=preserved,
            ratio=preserved / len(facts),
            missing=missing,
            summary_text=summary,
            fallback_used=False,
        )


__all__ = ["CompactionLossMetric", "LossResult"]
