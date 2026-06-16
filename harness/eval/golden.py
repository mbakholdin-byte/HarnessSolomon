"""Phase 3 B-mini: golden test data structures.

Defines ``GoldenFact`` — frozen dataclass for a marked fact inserted into
a seed session, plus JSONL loaders. The fixtures live in
``tests/eval/fixtures/`` (generated programmatically via the conftest
fixtures to avoid 50-line JSONL drift).

**Trust boundary:** Только stdlib (``dataclasses``, ``json``, ``pathlib``)
+ ``harness.memory.schema.Memory`` (read-only). NO imports from
``harness.agents``, ``harness.server``, ``harness.context``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


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


__all__ = [
    "GoldenFact",
    "load_golden_facts",
    "load_session_messages",
]
