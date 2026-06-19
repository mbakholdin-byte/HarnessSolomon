"""Phase 5 B2 + B3: Precision@5 and Recall@20 retrieval metrics.

``PrecisionMetric`` and ``RecallMetric`` measure BM25Retriever quality
on a golden query set. B2 = precision@5 ≥ 0.7, B3 = recall@20 ≥ 0.85,
both on the **subset** of 40 factual_lookup + paraphrased queries
(multi-hop is reported in ``per_category`` but NOT counted in the
main threshold — per ``docs/PHASE5-B2-B3-PLAN.md`` §5.1 B1, B5 + Марк
sign-off 2026-06-16).

**Algorithm:**
  1. Build ``Memory`` corpus from a session (one ``Memory`` per message,
     same pattern as ``harness/eval/retention.py``).
  2. Build a single ``BM25Retriever`` over the corpus (R5 fix: cache
     retriever for the whole measure call, not per-query).
  3. Map each ``GoldenQuery``'s ``relevant_fact_ids`` to the underlying
     ``Memory.id`` set via ``fact_id_to_relevant_memory_id`` (Phase 5
     B2 fix: ``turn_index``-based, NOT phrase substring).
  4. For each query, ``retriever.retrieve(query, k=self._k)`` returns
     the top-k ``(Memory, score)`` tuples.
  5. **Precision@5** (B2): per-query = ``|retrieved ∩ ground_truth| / k``,
     micro-average = ``Σ|retrieved ∩ gt| / (k * n_queries)``.
  6. **Recall@20** (B3): per-query = ``|retrieved ∩ gt| / |gt|``,
     micro-average = ``Σ|retrieved ∩ gt| / Σ|gt|``.
  7. Multi-hop queries (and any query with empty ground truth) are
     reported in ``per_category`` but **skipped** from the main
     numerator/denominator to keep the threshold meaningful for
     single-fact lookup.

**Trust boundary:** Импортирует ``harness.eval.golden``,
``harness.memory.retrieval.bm25.BM25Retriever``,
``harness.memory.schema.Memory``, и stdlib. НЕ импортирует
``harness.agents``, ``harness.server``, ``harness.context``, или
``harness.config``. Auto-checked by
``tests/eval/test_eval_trust_boundary.py`` (parametrized over all
``harness/eval/**/*.py``).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from harness.eval.golden import (
    GoldenFact,
    GoldenQuery,
    fact_id_to_relevant_memory_id,
)
from harness.memory.retrieval.bm25 import BM25Retriever
from harness.memory.schema import Memory


# Categories excluded from the main DoD threshold (B2 / B3 policy:
# multi-hop is BM25-sparse, reported separately for diagnostic).
_THRESHOLD_EXCLUDED_CATEGORIES = frozenset({"multi_hop"})


@dataclass(frozen=True)
class PrecisionResult:
    """Outcome of one ``PrecisionMetric.measure`` call.

    Attributes:
        total_queries: Queries that contributed to the main threshold
            (factual_lookup + paraphrased; multi-hop skipped).
        threshold_relevant_in_top5: Sum of ``|retrieved ∩ gt|`` across
            threshold queries.
        threshold_top5: ``k * total_queries`` (denominator).
        threshold_ratio: ``threshold_relevant_in_top5 / threshold_top5``
            (micro-average precision@k).
        per_query: ``{query_id: precision_at_k}`` for ALL queries
            (including multi-hop, for diagnostic).
        per_category: ``{category: mean precision_at_k}`` — includes
            ALL queries.
        per_difficulty: ``{difficulty: mean precision_at_k}`` —
            includes ALL queries.
        missed: Queries with ``precision_at_k < 1.0`` (subset of
            threshold queries; multi-hop NOT in this list).
        threshold_target: The target precision (e.g. 0.7) for reference.
        k: The k used for ``precision@k``.
    """

    total_queries: int
    threshold_relevant_in_top5: int
    threshold_top5: int
    threshold_ratio: float
    per_query: dict[str, float] = field(default_factory=dict)
    per_category: dict[str, float] = field(default_factory=dict)
    per_difficulty: dict[str, float] = field(default_factory=dict)
    missed: list[GoldenQuery] = field(default_factory=list)
    threshold_target: float = 0.7
    k: int = 5

    def __post_init__(self) -> None:
        if self.threshold_top5 > 0 and abs(
            self.threshold_ratio
            - self.threshold_relevant_in_top5 / self.threshold_top5
        ) > 1e-9:
            raise ValueError(
                f"threshold_ratio {self.threshold_ratio} != "
                f"relevant_in_top5 / top5 "
                f"({self.threshold_relevant_in_top5}/{self.threshold_top5})"
            )


@dataclass(frozen=True)
class RecallResult:
    """Outcome of one ``RecallMetric.measure`` call.

    Attributes:
        total_queries: Queries that contributed to the main threshold.
        threshold_relevant_retrieved: Sum of ``|retrieved ∩ gt|`` across
            threshold queries.
        threshold_relevant_in_ground_truth: Sum of ``|gt|`` across
            threshold queries.
        threshold_ratio: ``threshold_relevant_retrieved /
            threshold_relevant_in_ground_truth`` (micro-average recall@k).
        per_query: ``{query_id: recall_at_k}`` for ALL queries.
        per_category: ``{category: mean recall_at_k}``.
        per_difficulty: ``{difficulty: mean recall_at_k}``.
        missed: Queries with ``recall_at_k < 1.0`` (subset of threshold
            queries).
        threshold_target: The target recall (e.g. 0.85).
        k: The k used for ``recall@k``.
    """

    total_queries: int
    threshold_relevant_retrieved: int
    threshold_relevant_in_ground_truth: int
    threshold_ratio: float
    per_query: dict[str, float] = field(default_factory=dict)
    per_category: dict[str, float] = field(default_factory=dict)
    per_difficulty: dict[str, float] = field(default_factory=dict)
    missed: list[GoldenQuery] = field(default_factory=list)
    threshold_target: float = 0.85
    k: int = 20

    def __post_init__(self) -> None:
        if self.threshold_relevant_in_ground_truth > 0 and abs(
            self.threshold_ratio
            - self.threshold_relevant_retrieved
            / self.threshold_relevant_in_ground_truth
        ) > 1e-9:
            raise ValueError(
                f"threshold_ratio {self.threshold_ratio} != "
                f"relevant_retrieved / relevant_in_ground_truth "
                f"({self.threshold_relevant_retrieved}/"
                f"{self.threshold_relevant_in_ground_truth})"
            )


# === Corpus builder =====================================================


#: Canonical channel names. ``user`` = user prompts, ``assistant`` =
#: assistant responses, ``tool`` = tool results. The default corpus
#: includes ``user`` + ``tool`` (``assistant`` is excluded unless the
#: caller opts in — assistant turns are typically filler/ack that
#: pollute BM25 precision).
CHANNEL_USER: str = "user"
CHANNEL_ASSISTANT: str = "assistant"
CHANNEL_TOOL: str = "tool"

#: Default channel set for precision/recall evaluation: user + tool.
DEFAULT_CHANNELS: tuple[str, ...] = (CHANNEL_USER, CHANNEL_TOOL)


def _channel_for_message(msg: dict) -> str:
    """Map an OpenAI-shape message to its corpus channel.

    - ``user``     → CHANNEL_USER
    - ``assistant`` → CHANNEL_ASSISTANT (includes ``tool_calls``)
    - ``tool``     → CHANNEL_TOOL
    - ``system``   → CHANNEL_USER (system prompt participates in user
      channel — it is the seed context the user expects to retrieve
      against). ``system`` is rare in retrieval corpora, but mapping
      it to ``user`` (not ``assistant``) preserves the Phase 5 B2
      contract that system-prompt facts are retrievable via user-channel
      queries.
    """
    role = str(msg.get("role", "")).lower()
    if role == "assistant":
        return CHANNEL_ASSISTANT
    if role == "tool":
        return CHANNEL_TOOL
    # ``user`` and ``system`` both land in the user channel.
    return CHANNEL_USER


def session_to_corpus(
    session: list[dict],
    *,
    include_assistant_channel: bool = False,
) -> dict[str, list[Memory]]:
    """Convert an OpenAI-shape session into a **channel-separated** corpus.

    Phase 5.2A v1.24.0: the corpus is returned as a
    ``dict[channel_name, list[Memory]]`` instead of a flat
    ``list[Memory]``. The default channel set is
    ``{user, tool}`` (assistant excluded as filler); pass
    ``include_assistant_channel=True`` to add the assistant channel.

    One ``Memory`` per message, with ``content = json.dumps(msg)``
    (same pattern as ``harness/eval/retention.py``). The ``Memory.id``
    is stable: ``f"m{global_index}"`` where ``global_index`` counts
    every message in the session (including assistant turns — so the
    ids match the legacy flat-corpus ids, and ``fact_id_to_relevant_memory_id``
    mappings remain valid).

    Args:
        session: OpenAI-shape chat history (list of ``{role, content}``
            dicts).
        include_assistant_channel: When ``True``, the returned dict
            includes the ``assistant`` channel (assistant responses +
            assistant turns carrying ``tool_calls``). When ``False``
            (default), assistant turns are NOT placed in the corpus
            dict (but their ids still count toward the global index
            so user/tool ids are unaffected).

    Returns:
        ``dict`` mapping channel name (``"user"``, ``"assistant"``,
        ``"tool"``) to a list of ``Memory`` records. Channels with
        zero messages are absent from the dict (callers can use
        ``.get(channel, [])`` for safe access).
    """
    corpus: dict[str, list[Memory]] = {}
    for i, msg in enumerate(session):
        channel = _channel_for_message(msg)
        if channel == CHANNEL_ASSISTANT and not include_assistant_channel:
            continue
        mem = Memory(
            id=f"m{i}",
            content=json.dumps(msg, ensure_ascii=False),
            layer="L2",
            source="manual",
            metadata={"channel": channel},
        )
        corpus.setdefault(channel, []).append(mem)
    return corpus


def flatten_corpus(
    corpus: dict[str, list[Memory]] | list[Memory],
    *,
    channels: list[str] | None = None,
) -> list[Memory]:
    """Flatten a channel-separated corpus into a ``list[Memory]``.

    Phase 5.2A v1.24.0 helper. Accepts either:

      * a ``dict[channel, list[Memory]]`` returned by
        :func:`session_to_corpus` — flattens the requested channels
        (default: ALL channels in the dict, in insertion order), OR
      * a legacy ``list[Memory]`` — returned unchanged when
        ``channels`` is ``None``, filtered in-place when ``channels``
        is set (each Memory's channel is inferred from its
        ``metadata["channel"]`` if present).

    Deduplicates by ``Memory.id`` (preserves first occurrence) so
    a Memory appearing in two channels (rare) does not inflate the
    retriever's corpus.

    Args:
        corpus:   Channel dict (preferred) or legacy flat list.
        channels: Optional channel filter. When ``None``, all channels
            in the dict are included (backward-compat with legacy
            callers that want the full corpus).

    Returns:
        Flat ``list[Memory]`` suitable for ``BM25Retriever`` /
        ``DenseRetriever`` / ``HybridRetriever``.
    """
    if isinstance(corpus, dict):
        if channels is None:
            # Include all channels in insertion order.
            ordered: list[Memory] = []
            for mems in corpus.values():
                ordered.extend(mems)
        else:
            ordered = []
            for ch in channels:
                ordered.extend(corpus.get(ch, []))
    else:
        # Legacy list[Memory] — filter by metadata["channel"] when
        # channels is set, otherwise return as-is.
        if channels is None:
            return list(corpus)
        ordered = [
            m for m in corpus
            if m.metadata.get("channel") in channels
        ]
    # Dedup by id preserving first occurrence.
    seen: set[str] = set()
    out: list[Memory] = []
    for m in ordered:
        if m.id in seen:
            continue
        seen.add(m.id)
        out.append(m)
    return out


# === Precision @ k =====================================================


class PrecisionMetric:
    """B2 — measure ``precision@k`` on a golden query set.

    Phase 5.2A v1.24.0: accepts either a flat ``list[Memory]``
    (legacy) or a channel-separated ``dict[str, list[Memory]]``
    returned by :func:`session_to_corpus`. When ``channels`` is set,
    only the specified channels contribute to the BM25 corpus.

    Phase 5.2B v1.24.0: filler detection + length-normalised
    re-ranking are integrated into the retrieve→score pipeline:

        1. ``retriever.retrieve(query, k=fetch_k)`` — BM25 top-N
           (``fetch_k`` is larger than ``k`` so the re-ranker has
           room to reorder; default ``fetch_k = k * 4``).
        2. ``FillerDetector.filter_fillers(retrieved)`` — drop
           LLM preambles / too-short / too-long docs.
        3. ``LengthNormalizedReranker.rerank(query, filtered)`` —
           dampen extreme-length outliers.
        4. Take top-``k`` from the re-ranked list → compute precision.

    Both stages are opt-in via the constructor flags so the metric
    stays backwards compatible with the Phase 5 B2 baseline.

    Usage::

        metric = PrecisionMetric(k=5)
        corpus = session_to_corpus(session)  # dict[str, list[Memory]]
        result = metric.measure(corpus, queries, facts)
        assert result.threshold_ratio >= 0.7  # B2 DoD
    """

    def __init__(
        self,
        k: int = 5,
        threshold_target: float = 0.7,
        *,
        channels: list[str] | None = None,
        use_filler_filter: bool = True,
        use_reranker: bool = True,
        fetch_k_multiplier: int = 4,
    ) -> None:
        if k <= 0:
            raise ValueError(f"k must be > 0, got {k}")
        if not 0.0 <= threshold_target <= 1.0:
            raise ValueError(
                f"threshold_target must be in [0, 1], got {threshold_target}"
            )
        if fetch_k_multiplier < 1:
            raise ValueError(
                f"fetch_k_multiplier must be >= 1, got {fetch_k_multiplier}"
            )
        self._k = k
        self._threshold_target = threshold_target
        # Phase 5.2A v1.24.0: optional channel filter. When ``None``,
        # all channels in the dict are included (backward-compat with
        # legacy callers that want the full corpus).
        self._channels = list(channels) if channels is not None else None
        # Phase 5.2B v1.24.0: filler + reranker integration.
        self._use_filler_filter = bool(use_filler_filter)
        self._use_reranker = bool(use_reranker)
        self._fetch_k = max(k, k * fetch_k_multiplier)
        # Late imports — preserves the trust boundary (filler.py and
        # reranker.py import only harness.memory.schema + stdlib).
        self._filler = None
        self._reranker = None
        if self._use_filler_filter:
            from harness.eval.filler import FillerDetector
            self._filler = FillerDetector()
        if self._use_reranker:
            from harness.eval.reranker import LengthNormalizedReranker
            self._reranker = LengthNormalizedReranker()

    def _retrieve_and_postprocess(
        self,
        retriever: BM25Retriever,
        query: str,
    ) -> list[tuple[Memory, float]]:
        """Retrieve → filter fillers → rerank → top-k.

        Phase 5.2B v1.24.0 pipeline. When both ``use_filler_filter``
        and ``use_reranker`` are False (or the feature flags are off),
        this collapses to the legacy ``retriever.retrieve(query, k=k)``
        call — backward compatible with the Phase 5 B2 baseline.
        """
        # When neither feature is enabled, use the legacy path.
        if not self._use_filler_filter and not self._use_reranker:
            return retriever.retrieve(query, k=self._k)
        # Fetch a larger candidate set so the re-ranker has room.
        fetched = retriever.retrieve(query, k=self._fetch_k)
        # Filler filter: drop LLM preambles / too-short / too-long.
        if self._filler is not None:
            fetched = [
                (m, s) for m, s in fetched
                if not self._filler.is_filler(m.content)
            ]
        # Re-ranker: length-normalised score, stable sort.
        if self._reranker is not None:
            fetched = self._reranker.rerank(query, fetched)
        # Take top-k.
        return fetched[: self._k]

    def measure(
        self,
        corpus: "dict[str, list[Memory]] | list[Memory]",
        queries: list[GoldenQuery],
        facts: list[GoldenFact],
    ) -> PrecisionResult:
        """Run ``precision@k`` on ``queries`` against ``corpus``.

        Args:
            corpus: Either a channel-separated dict (preferred, from
                ``session_to_corpus``) or a legacy flat list of
                ``Memory`` records.
            queries: Golden queries with ground-truth fact_ids.
            facts: Marked facts in the corpus (for fact_id → memory_id
                mapping via ``turn_index``).

        Returns:
            ``PrecisionResult`` with main threshold metrics (subset
            excluding multi-hop) and full per_category / per_difficulty
            breakdowns.
        """
        # Phase 5.2A v1.24.0: flatten channel-separated corpus.
        flat_corpus = flatten_corpus(corpus, channels=self._channels)
        if not queries:
            return PrecisionResult(
                total_queries=0,
                threshold_relevant_in_top5=0,
                threshold_top5=0,
                threshold_ratio=1.0,
                threshold_target=self._threshold_target,
                k=self._k,
            )
        # k > corpus check applies only when there are queries to
        # process (empty queries is a valid no-op return regardless
        # of corpus size). k must be ≤ corpus size because
        # precision@k divides by k, and retrieved_ids can have at
        # most ``len(corpus)`` elements. If k > corpus, precision
        # could exceed 1.0.
        if self._k > len(flat_corpus):
            raise ValueError(
                f"k={self._k} exceeds corpus size {len(flat_corpus)}"
            )
        retriever = BM25Retriever(flat_corpus)
        fact_to_mem = fact_id_to_relevant_memory_id(facts, flat_corpus)

        per_query: dict[str, float] = {}
        per_category_values: dict[str, list[float]] = defaultdict(list)
        per_difficulty_values: dict[str, list[float]] = defaultdict(list)
        threshold_relevant = 0
        threshold_top5 = 0
        missed: list[GoldenQuery] = []

        for q in queries:
            retrieved = self._retrieve_and_postprocess(
                retriever, q.query,
            )
            retrieved_ids = {m.id for m, _score in retrieved}

            # Ground truth: union of Memory ids for each relevant fact.
            ground_truth_ids: set[str] = {
                fact_to_mem[fid]
                for fid in q.relevant_fact_ids
                if fact_to_mem.get(fid)  # skip empty (turn_index OOB)
            }

            if not ground_truth_ids:
                # No ground truth for this query — skip from per_query
                # stats entirely (would give precision 0 by default).
                continue

            relevant_in_topk = len(retrieved_ids & ground_truth_ids)
            precision = relevant_in_topk / self._k
            per_query[q.id] = precision
            per_category_values[q.category].append(precision)
            per_difficulty_values[q.difficulty].append(precision)

            if q.category not in _THRESHOLD_EXCLUDED_CATEGORIES:
                threshold_relevant += relevant_in_topk
                threshold_top5 += self._k
                if precision < 1.0:
                    missed.append(q)

        threshold_ratio = (
            threshold_relevant / max(threshold_top5, 1)
        )

        # Aggregate per_category / per_difficulty means.
        per_category = {
            cat: sum(vals) / len(vals) for cat, vals in per_category_values.items()
        }
        per_difficulty = {
            diff: sum(vals) / len(vals)
            for diff, vals in per_difficulty_values.items()
        }

        return PrecisionResult(
            total_queries=len(queries),
            threshold_relevant_in_top5=threshold_relevant,
            threshold_top5=threshold_top5,
            threshold_ratio=threshold_ratio,
            per_query=per_query,
            per_category=per_category,
            per_difficulty=per_difficulty,
            missed=missed,
            threshold_target=self._threshold_target,
            k=self._k,
        )


# === Recall @ k ========================================================


class RecallMetric:
    """B3 — measure ``recall@k`` on a golden query set.

    Phase 5.2A v1.24.0: accepts either a flat ``list[Memory]``
    (legacy) or a channel-separated ``dict[str, list[Memory]]``.
    See :class:`PrecisionMetric` for the channel parameter semantics.

    Usage::

        metric = RecallMetric(k=20)
        corpus = session_to_corpus(session)
        result = metric.measure(corpus, queries, facts)
        assert result.threshold_ratio >= 0.85  # B3 DoD
    """

    def __init__(
        self,
        k: int = 20,
        threshold_target: float = 0.85,
        *,
        channels: list[str] | None = None,
    ) -> None:
        if k <= 0:
            raise ValueError(f"k must be > 0, got {k}")
        if not 0.0 <= threshold_target <= 1.0:
            raise ValueError(
                f"threshold_target must be in [0, 1], got {threshold_target}"
            )
        self._k = k
        self._threshold_target = threshold_target
        self._channels = list(channels) if channels is not None else None

    def measure(
        self,
        corpus: "dict[str, list[Memory]] | list[Memory]",
        queries: list[GoldenQuery],
        facts: list[GoldenFact],
    ) -> RecallResult:
        """Run ``recall@k`` on ``queries`` against ``corpus``."""
        # Phase 5.2A v1.24.0: flatten channel-separated corpus.
        flat_corpus = flatten_corpus(corpus, channels=self._channels)
        if not queries:
            return RecallResult(
                total_queries=0,
                threshold_relevant_retrieved=0,
                threshold_relevant_in_ground_truth=0,
                threshold_ratio=1.0,
                threshold_target=self._threshold_target,
                k=self._k,
            )
        # k > corpus check: see PrecisionMetric.measure for rationale.
        if self._k > len(flat_corpus):
            raise ValueError(
                f"k={self._k} exceeds corpus size {len(flat_corpus)}"
            )
        retriever = BM25Retriever(flat_corpus)
        fact_to_mem = fact_id_to_relevant_memory_id(facts, flat_corpus)

        per_query: dict[str, float] = {}
        per_category_values: dict[str, list[float]] = defaultdict(list)
        per_difficulty_values: dict[str, list[float]] = defaultdict(list)
        threshold_relevant_retrieved = 0
        threshold_relevant_in_gt = 0
        missed: list[GoldenQuery] = []

        for q in queries:
            retrieved = retriever.retrieve(q.query, k=self._k)
            retrieved_ids = {m.id for m, _score in retrieved}

            ground_truth_ids: set[str] = {
                fact_to_mem[fid]
                for fid in q.relevant_fact_ids
                if fact_to_mem.get(fid)
            }

            if not ground_truth_ids:
                continue

            relevant_retrieved = len(retrieved_ids & ground_truth_ids)
            recall = relevant_retrieved / len(ground_truth_ids)
            per_query[q.id] = recall
            per_category_values[q.category].append(recall)
            per_difficulty_values[q.difficulty].append(recall)

            if q.category not in _THRESHOLD_EXCLUDED_CATEGORIES:
                threshold_relevant_retrieved += relevant_retrieved
                threshold_relevant_in_gt += len(ground_truth_ids)
                if recall < 1.0:
                    missed.append(q)

        threshold_ratio = (
            threshold_relevant_retrieved
            / max(threshold_relevant_in_gt, 1)
        )

        per_category = {
            cat: sum(vals) / len(vals) for cat, vals in per_category_values.items()
        }
        per_difficulty = {
            diff: sum(vals) / len(vals)
            for diff, vals in per_difficulty_values.items()
        }

        return RecallResult(
            total_queries=len(queries),
            threshold_relevant_retrieved=threshold_relevant_retrieved,
            threshold_relevant_in_ground_truth=threshold_relevant_in_gt,
            threshold_ratio=threshold_ratio,
            per_query=per_query,
            per_category=per_category,
            per_difficulty=per_difficulty,
            missed=missed,
            threshold_target=self._threshold_target,
            k=self._k,
        )


__all__ = [
    "PrecisionMetric",
    "RecallMetric",
    "PrecisionResult",
    "RecallResult",
    "session_to_corpus",
    "flatten_corpus",
    "CHANNEL_USER",
    "CHANNEL_ASSISTANT",
    "CHANNEL_TOOL",
    "DEFAULT_CHANNELS",
]
