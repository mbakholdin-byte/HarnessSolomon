"""Phase 5 B2/B3: structural tests for GoldenQuery, load_golden_queries, fact_id_to_relevant_memory_id.

These tests verify the **infrastructure** for B2/B3 (data structures
and helpers), not the metric output. Companion to test_precision_golden
and test_recall_golden which test the metrics themselves.

Test scope (5 tests):
    - test_golden_query_rejects_oversized_relevant: 1-3 fact_ids.
    - test_golden_query_rejects_overlap: relevant ∩ irrelevant == ∅.
    - test_load_golden_queries_round_trip: 20 manual queries load.
    - test_fact_id_to_relevant_memory_id_uses_turn_index: mapping
      uses ``f.turn_index + 1`` (offset for system message).
    - test_fact_id_to_relevant_memory_id_out_of_bounds: returns
      empty string for OOB turn_index (defensive).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.eval import GoldenFact, GoldenQuery
from harness.eval.golden import (
    fact_id_to_relevant_memory_id,
    load_golden_queries,
)
from harness.eval.retrieval import session_to_corpus
from harness.memory.schema import Memory


def test_golden_query_rejects_oversized_relevant() -> None:
    """GoldenQuery requires 1-3 relevant_fact_ids."""
    with pytest.raises(ValueError, match="relevant_fact_ids"):
        GoldenQuery(
            id="Q_BAD",
            query="test",
            relevant_fact_ids=(),  # 0 — invalid
            irrelevant_fact_ids=("F01",),
            category="factual_lookup",
            difficulty="easy",
        )
    with pytest.raises(ValueError, match="relevant_fact_ids"):
        GoldenQuery(
            id="Q_BAD",
            query="test",
            relevant_fact_ids=("F01", "F02", "F03", "F04"),  # 4 — invalid
            irrelevant_fact_ids=(),
            category="factual_lookup",
            difficulty="easy",
        )


def test_golden_query_rejects_overlap() -> None:
    """Relevant and irrelevant sets must be disjoint."""
    with pytest.raises(ValueError, match="overlap"):
        GoldenQuery(
            id="Q_BAD",
            query="test",
            relevant_fact_ids=("F01", "F02"),
            irrelevant_fact_ids=("F02", "F03"),  # F02 in both
            category="factual_lookup",
            difficulty="easy",
        )


def test_load_golden_queries_round_trip() -> None:
    """20 manual queries load from JSONL fixture."""
    path = Path(__file__).parent / "fixtures" / "golden_queries.jsonl"
    queries = load_golden_queries(path)
    assert len(queries) == 20, f"expected 20 manual queries, got {len(queries)}"
    # Distribution: 10 factual_lookup + 5 paraphrased + 5 multi_hop.
    from collections import Counter

    counts = Counter(q.category for q in queries)
    assert counts["factual_lookup"] == 10
    assert counts["paraphrased"] == 5
    assert counts["multi_hop"] == 5
    # IDs are unique.
    assert len({q.id for q in queries}) == 20


def test_fact_id_to_relevant_memory_id_uses_phrase_substring(
    golden_facts: list[GoldenFact],
    seed_session_100: list[dict],
) -> None:
    """Mapping uses phrase substring scan (robust to session structure).

    The standard ``seed_session_100`` has 2 messages per turn (user
    + assistant), so the original ``turn_index + 1`` offset from the
    plan was wrong. Phrase substring mapping works regardless of
    session shape — each fact.phrase appears in exactly one user
    message (B1 design rule).
    """
    corpus = session_to_corpus(seed_session_100)
    mapping = fact_id_to_relevant_memory_id(golden_facts, corpus)

    assert len(mapping) == len(golden_facts)
    # F01 has phrase "Phase 3 v1.5.0" → found in some user message.
    assert mapping["F01"] != "", f"F01 should map to a memory; got {mapping['F01']!r}"
    assert mapping["F01"].startswith("m"), (
        f"F01 should map to a Memory id; got {mapping['F01']!r}"
    )
    # All 50 facts should map (defensive — should not happen with
    # the standard seed session).
    for f in golden_facts:
        assert mapping[f.id] != "", (
            f"Fact {f.id} (phrase={f.phrase!r}) did not match any corpus memory"
        )


def test_fact_id_to_relevant_memory_id_phrase_not_found() -> None:
    """Phrase not in corpus → empty string (defensive)."""
    facts = [
        GoldenFact(id="F_PRESENT", phrase="needle", turn_index=0, category="user"),
        GoldenFact(id="F_ABSENT", phrase="haystack missing", turn_index=1, category="user"),
    ]
    corpus = session_to_corpus([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "this is a needle in a haystack"},
    ])
    mapping = fact_id_to_relevant_memory_id(facts, corpus)

    assert mapping["F_PRESENT"] == "m1"  # first match wins
    assert mapping["F_ABSENT"] == ""
