"""Phase 3 B-mini + Phase 5 B2/B3: golden test data structures.

Defines ``GoldenFact`` — frozen dataclass for a marked fact inserted into
a seed session, plus JSONL loaders. The fixtures live in
``tests/eval/fixtures/`` (generated programmatically via the conftest
fixtures to avoid 50-line JSONL drift).

Phase 5 B2/B3 (16.06.2026) adds:
    - ``GoldenQuery`` — frozen dataclass for a retrieval test query
      with ground-truth fact_ids.
    - ``load_golden_queries`` — JSONL loader (mirror of
      ``load_golden_facts``).
    - ``fact_id_to_relevant_memory_id`` — map ``GoldenFact.id`` →
      the **single** Memory id in a corpus where the fact was seeded
      (via ``turn_index``). B2/B3 use this to build a deterministic
      ground truth WITHOUT substring scanning (B2 fix from
      ``docs/PHASE5-B2-B3-PLAN.md`` §5.1).

**Trust boundary:** Только stdlib (``dataclasses``, ``json``, ``pathlib``)
+ ``harness.memory.schema.Memory`` (read-only). NO imports from
``harness.agents``, ``harness.server``, ``harness.context``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from harness.memory.schema import Memory


@dataclass(frozen=True)
class GoldenFact:
    """A marked fact in a seed session that the metric must recover.

    Attributes:
        id: Stable id (e.g. "F01"). Used as a key in
            ``RetentionResult.top_doc_ids`` and ``LossResult.missing``.
        phrase: Substring that must appear in the retrieved Memory
            content (B1) or in the summary message (B4). Case-insensitive
            match. Phrase should be **specific** (e.g. "Qdrant primary",
            "Phase 3 v1.5.0") so BM25 can lift it above generic words.
        turn_index: Zero-based index of the message in the seed session
            where the phrase is inserted. Used to distribute facts
            uniformly (early / mid / late) per C2.
        category: Origin of the fact. Helps test coverage: "user"
            (LLM context), "tool_result" (sub-agent output), "scratchpad"
            (L0/L1 notes — Phase 3 v1.2.0+).
    """

    id: str
    phrase: str
    turn_index: int
    category: Literal["user", "tool_result", "scratchpad"]


# Categories for golden queries. ``factual_lookup`` is a direct BM25 hit,
# ``paraphrased`` swaps words for synonyms (still single-fact), and
# ``multi_hop`` requires 2-3 facts to answer (BM25 sparse, known weakness).
_QUERY_CATEGORY = Literal["factual_lookup", "paraphrased", "multi_hop"]
_QUERY_DIFFICULTY = Literal["easy", "medium", "hard"]


@dataclass(frozen=True)
class GoldenQuery:
    """A retrieval test query with ground-truth fact_ids.

    B2 (precision@5) and B3 (recall@20) measure BM25Retriever's ability
    to surface the right ``Memory`` records for a natural-language
    question.

    Attributes:
        id: Stable id (e.g. "Q01"). Mirrors the GoldenFact id space.
        query: Natural-language question, 5-12 words. BM25-friendly
            phrasing (use the same terms that appear in the
            relevant message, optionally with synonyms).
        relevant_fact_ids: 1-3 ``GoldenFact.id`` that should be in the
            retrieved set. **Ground truth** for precision (subset of
            top-5) and recall (subset of top-20).
        irrelevant_fact_ids: 4-6 ``GoldenFact.id`` that should NOT be
            in the retrieved set. Used for human inspection of
            ``PrecisionResult.missed`` — NOT used in the metric
            calculation (per plan §5.3 C3).
        category: Query type. ``multi_hop`` is reported separately
            (per_category breakdown) and NOT included in the main
            DoD threshold (per plan §5.1 B1, B5 + Марк sign-off 2026-06-16).
        difficulty: BM25 token overlap with the source phrase. ``easy``
            ≥60%, ``medium`` 30-60%, ``hard`` <30% (multi-hop or
            paraphrase). Used in ``per_difficulty`` breakdown.
    """

    id: str
    query: str
    relevant_fact_ids: tuple[str, ...]
    irrelevant_fact_ids: tuple[str, ...]
    category: _QUERY_CATEGORY
    difficulty: _QUERY_DIFFICULTY

    def __post_init__(self) -> None:
        if not 1 <= len(self.relevant_fact_ids) <= 3:
            raise ValueError(
                f"GoldenQuery {self.id!r}: relevant_fact_ids must be 1-3, "
                f"got {len(self.relevant_fact_ids)}"
            )
        if not 0 <= len(self.irrelevant_fact_ids) <= 8:
            raise ValueError(
                f"GoldenQuery {self.id!r}: irrelevant_fact_ids must be 0-8, "
                f"got {len(self.irrelevant_fact_ids)}"
            )
        overlap = set(self.relevant_fact_ids) & set(self.irrelevant_fact_ids)
        if overlap:
            raise ValueError(
                f"GoldenQuery {self.id!r}: fact_ids overlap between "
                f"relevant and irrelevant: {overlap}"
            )


def load_golden_facts(path: Path) -> list[GoldenFact]:
    """Load golden facts from a JSONL file.

    Each line must be a JSON object with fields ``id``, ``phrase``,
    ``turn_index`` (int), and ``category`` (one of "user", "tool_result",
    "scratchpad"). Malformed lines raise ``ValueError``.

    Args:
        path: Path to a JSONL file. Lines starting with ``#`` are
            treated as comments and skipped (mirror of
            ``harness/memory/retrieval/bm25.py`` fixture convention).
    """
    if not path.exists():
        raise FileNotFoundError(f"golden facts file not found: {path}")
    facts: list[GoldenFact] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno} invalid JSON: {e}") from e
        try:
            facts.append(GoldenFact(
                id=obj["id"],
                phrase=obj["phrase"],
                turn_index=int(obj["turn_index"]),
                category=obj["category"],
            ))
        except KeyError as e:
            raise ValueError(f"{path}:{lineno} missing field: {e}") from e
    return facts


def load_golden_queries(path: Path) -> list[GoldenQuery]:
    """Load golden queries from a JSONL file (Phase 5 B2/B3).

    Each line must be a JSON object with fields ``id``, ``query``,
    ``relevant_fact_ids`` (list of str), ``irrelevant_fact_ids`` (list
    of str), ``category`` ("factual_lookup" | "paraphrased" |
    "multi_hop"), and ``difficulty`` ("easy" | "medium" | "hard").
    Malformed lines raise ``ValueError``.

    Args:
        path: Path to a JSONL file. Lines starting with ``#`` are
            treated as comments and skipped (mirror of
            ``load_golden_facts``).
    """
    if not path.exists():
        raise FileNotFoundError(f"golden queries file not found: {path}")
    queries: list[GoldenQuery] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno} invalid JSON: {e}") from e
        try:
            queries.append(GoldenQuery(
                id=obj["id"],
                query=obj["query"],
                relevant_fact_ids=tuple(obj["relevant_fact_ids"]),
                irrelevant_fact_ids=tuple(obj["irrelevant_fact_ids"]),
                category=obj["category"],
                difficulty=obj["difficulty"],
            ))
        except KeyError as e:
            raise ValueError(f"{path}:{lineno} missing field: {e}") from e
        except ValueError as e:
            # Re-raise from GoldenQuery.__post_init__ with line context.
            raise ValueError(f"{path}:{lineno} {e}") from e
    return queries


def load_session_messages(path: Path) -> list[dict[str, Any]]:
    """Load an OpenAI-shape chat history from a JSONL file.

    Each line is a JSON object with at least ``role`` and ``content``.
    Used for pre-recorded sessions in the fixtures directory. Most
    B-mini tests generate sessions programmatically (see
    ``tests/eval/conftest.py:seed_session_100``) — this loader is for
    the rare case of a hand-recorded fixture.
    """
    if not path.exists():
        raise FileNotFoundError(f"session file not found: {path}")
    messages: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno} invalid JSON: {e}") from e
    return messages


def fact_id_to_relevant_memory_id(
    facts: list[GoldenFact],
    corpus: list[Memory],
) -> dict[str, str]:
    """Map ``GoldenFact.id`` → single ``Memory.id`` via phrase substring.

    Phase 5 B2 implementation note (revised from initial plan):
    The original plan (§5.1 B2) proposed ``turn_index + 1`` as the
    Memory index, which only works for sessions with 1 message per
    turn. ``seed_session_100`` has 2 messages per turn (user +
    assistant), so the correct offset is ``2*turn_index + 1`` (user)
    or ``2*turn_index + 2`` (assistant) — the helper cannot know
    the session shape a priori.

    Phrase substring mapping is **robust to session structure** as
    long as phrases are specific (per B1 design rule). For the
    standard ``seed_session_100``, each fact.phrase appears in
    exactly one ``Memory.content`` (the user message that seeded it),
    so the substring scan returns exactly one match. If a phrase
    ever appears in multiple messages (e.g. duplicated in summary),
    the first match wins — golden queries reference the seed
    message, not the duplicate.

    Performance: O(facts × corpus) substring scans. For 50 facts ×
    205 messages = ~10K operations, sub-millisecond on the standard
    fixture.

    Args:
        facts: Marked facts in the seed session.
        corpus: ``Memory`` records built from the same seed session
            (one Memory per message, in order). The B-mini pattern is
            ``Memory(id=f"m{i}", content=json.dumps(msg), ...)``.

    Returns:
        ``{fact_id: memory_id}``. If a phrase is not found in the
        corpus (defensive — should not happen with the standard
        100-turn session), the fact maps to an empty string and the
        metric skips that query.
    """
    result: dict[str, str] = {}
    for f in facts:
        phrase_lower = f.phrase.lower()
        match: str | None = None
        for m in corpus:
            if phrase_lower in m.content.lower():
                match = m.id
                break
        result[f.id] = match or ""
    return result


__all__ = [
    "GoldenFact",
    "GoldenQuery",
    "load_golden_facts",
    "load_golden_queries",
    "load_session_messages",
    "fact_id_to_relevant_memory_id",
]
