"""Phase 5.2B — Filler detector (stdlib-only, no ML).

Identifies and filters "filler" documents from retrieval results
before precision/recall scoring. Fillers are documents that BM25
retrieves with a high lexical match but carry no factual signal —
typically LLM-style preambles ("Sure, let me help with that"),
acknowledgements, or empty/garbage turns.

Three heuristic families (all pure-Python, zero deps):

  1. **Length filter** — documents shorter than ``min_doc_len`` chars
     or longer than ``max_doc_len`` chars are treated as fillers.
     The BM25 score of a 5-char document is meaningless (any token
     match saturates the score); a 5000-char dump is usually a log
     paste, not a fact.
  2. **Lexical filter** — documents starting with common LLM filler
     phrases ("Sure", "Let me", "I'll", "I can") AND shorter than
     ``lexical_max_len`` chars are flagged. The length guard
     prevents false positives on genuine content that happens to
     start with "Sure enough, the API returns...".
  3. **Repetition filter** — documents where the same short sentence
     (≤ ``repetition_sentence_max_len`` chars) repeats 3+ times in
     a row are flagged. Catches "OK. OK. OK." / "Done. Done. Done."
     style outputs.

The detector is deliberately conservative — a false positive
(dropping a real fact) is worse than a false negative (keeping a
filler). The heuristics err on the side of passing through when
in doubt.

Config
------

``FillerDetectorConfig`` is a frozen dataclass with sensible
defaults. The detector is constructed once and reused across
queries (it's stateless after init).

Trust boundary
--------------

This module imports only :mod:`harness.memory.schema` (for the
``Memory`` type — the canonical document shape in the eval pipeline)
and stdlib. It does NOT import :mod:`harness.agents`,
:mod:`harness.server`, or any ML library. Auto-checked by
``tests/eval/test_eval_trust_boundary.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from harness.memory.schema import Memory


# === Protocol (duck-typed document) ====================================


class _DocumentLike(Protocol):
    """Structural type for anything with a ``content`` string.

    The detector works on any document-shaped object (``Memory``,
    a dataclass with ``content``, a dict wrapper) — we only ever
    read ``.content``. Using a Protocol instead of hard-binding to
    ``Memory`` keeps the detector reusable if the eval pipeline
    later adopts a different document type.
    """

    content: str


# === Config ============================================================


@dataclass(frozen=True)
class FillerDetectorConfig:
    """Configuration for :class:`FillerDetector`.

    Attributes:
        max_doc_len: Documents longer than this (chars) are fillers.
            Default 2000. Catches log dumps, stack traces pasted
            whole, etc.
        min_doc_len: Documents shorter than this (chars) are fillers.
            Default 30. Catches "OK", "Done", "Yes" turns.
        enable_lexical_heuristics: When True (default), apply the
            LLM-filler-phrase check. Disable for corpora where
            phrases like "Let me" are legitimate openers.
        lexical_max_len: Max length for the lexical heuristic to
            fire. A doc starting with "Sure" that's 500 chars long
            is probably real content; one that's 80 chars is a
            filler. Default 100.
        lexical_phrases: Tuple of phrase prefixes. Default covers
            the common LLM acknowledgements.
        repetition_min_count: Minimum consecutive repeats to flag.
            Default 3 ("OK. OK. OK." trips; "OK. OK." doesn't).
        repetition_sentence_max_len: Max sentence length for the
            repetition check. Only short, repeated sentences are
            fillers — a paragraph repeated 3x is unusual but not
            necessarily filler.
    """

    max_doc_len: int = 2000
    min_doc_len: int = 30
    enable_lexical_heuristics: bool = True
    lexical_max_len: int = 100
    lexical_phrases: tuple[str, ...] = field(
        default_factory=lambda: (
            "sure",
            "let me",
            "i'll",
            "i can",
            "of course",
            "certainly",
            "absolutely",
            "great question",
            "happy to help",
        )
    )
    repetition_min_count: int = 3
    repetition_sentence_max_len: int = 40


# === Detector ==========================================================


class FillerDetector:
    """Heuristic filler-document detector (no ML).

    Usage::

        detector = FillerDetector()
        if detector.is_filler(doc.content):
            ...  # skip
        clean = detector.filter_fillers(docs)

    The detector is stateless after construction — safe to share
    across threads / async tasks / queries.
    """

    def __init__(self, config: FillerDetectorConfig | None = None) -> None:
        self.config: FillerDetectorConfig = (
            config if config is not None else FillerDetectorConfig()
        )

    # --- single-doc API ---

    def is_filler(self, text: str) -> bool:
        """Return True if ``text`` looks like a filler document.

        Evaluated in order of cheapest-to-compute: length → lexical
        → repetition. Short-circuits on the first hit.
        """
        if not text:
            return True
        n = len(text)
        cfg = self.config
        # 1. Length filter.
        if n < cfg.min_doc_len or n > cfg.max_doc_len:
            return True
        # 2. Lexical filter (LLM filler phrases).
        if cfg.enable_lexical_heuristics and n <= cfg.lexical_max_len:
            lowered = text.lstrip().lower()
            for phrase in cfg.lexical_phrases:
                if lowered.startswith(phrase):
                    return True
        # 3. Repetition filter (3+ identical short sentences in a row).
        if self._has_repetition(text):
            return True
        return False

    def _has_repetition(self, text: str) -> bool:
        """Detect 3+ consecutive identical short sentences.

        Splits on ``. ``, ``!``, ``?`` (sentence-ish boundaries),
        strips whitespace, and looks for a run of
        ``repetition_min_count`` identical sentences where each is
        ≤ ``repetition_sentence_max_len`` chars. Returns False on
        any parse ambiguity (defensive — never crash on weird input).
        """
        cfg = self.config
        # Lightweight split — we don't need NLTK here. Keep the
        # delimiters out of the fragments.
        raw_parts: list[str] = []
        buf: list[str] = []
        for ch in text:
            if ch in ".!?\n":
                raw_parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        if buf:
            raw_parts.append("".join(buf))
        sentences = [p.strip().lower() for p in raw_parts if p.strip()]
        if len(sentences) < cfg.repetition_min_count:
            return False
        run_len = 1
        for i in range(1, len(sentences)):
            prev = sentences[i - 1]
            curr = sentences[i]
            if (
                curr == prev
                and len(curr) <= cfg.repetition_sentence_max_len
            ):
                run_len += 1
                if run_len >= cfg.repetition_min_count:
                    return True
            else:
                run_len = 1
        return False

    # --- batch API ---

    def filter_fillers(self, docs: list[Memory]) -> list[Memory]:
        """Return the subset of ``docs`` that are NOT fillers.

        Preserves input order. When the detector is disabled (all
        heuristics off via an empty config), this is a pass-through.
        """
        return [d for d in docs if not self.is_filler(d.content)]


__all__ = [
    "FillerDetector",
    "FillerDetectorConfig",
]
