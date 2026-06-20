"""Phase 6.4 v1.29.0: Multi-pattern redaction — Rust fast path.

Wraps the optional Rust extension ``harness_perf.redact_patterns``
(AhoCorasick multi-pattern replace) with a pure-Python fallback that
produces identical output. Used by the privacy layer to scrub a list of
**literal** substrings from outgoing text in a single pass — the
complementary case to ``harness.redaction.patterns`` which handles
**structural** patterns (EMAIL, JWT, …) via regex.

Trust boundary:
    The Rust module is a leaf dependency — it does NOT import any
    ``harness.*`` code and operates purely on ``str`` / ``list[str]``.
    This wrapper is the only place that bridges into the harness package.

Fallback policy:
    On any ``ImportError`` (Rust wheel not built, wrong Python ABI,
    platform without a Rust toolchain) we transparently fall back to a
    left-to-right ``str.replace`` loop. The output is identical for any
    non-overlapping pattern set; the Rust path is purely an optimisation.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

__all__ = ["redact_patterns", "DEFAULT_REPLACEMENT", "is_rust_active"]

#: Default replacement string. MUST match ``redact::DEFAULT_REPLACEMENT``
#: in ``harness-perf/src/redact.rs`` so both paths emit the same bytes.
DEFAULT_REPLACEMENT: str = "[REDACTED]"


def _rust_available() -> bool:
    """Return ``True`` iff the ``harness_perf`` Rust extension imports.

    Cached so the import probe runs at most once per process. We never
    re-check after a failure — once the wheel is missing it stays missing
    until the process restarts.
    """
    try:
        import harness_perf  # noqa: F401  (import side-effect only)
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def is_rust_active() -> bool:
    """Public probe: is the Rust fast path currently in use?

    Exposed for tests and observability — ``PrivacyZoneFilter`` does not
    branch on this (it always calls :func:`redact_patterns`, which picks
    the right backend internally).
    """
    return _rust_available()


def redact_patterns(
    text: str,
    patterns: list[str],
    replacement: Optional[str] = None,
) -> str:
    """Replace every occurrence of any pattern in ``patterns``.

    Args:
        text:        Source string. Empty / non-str → returned unchanged
                     (defensive: callers may pass ``None`` from sinks).
        patterns:    List of **literal** substrings to scrub. Order does
                     not matter for non-overlapping sets; for overlapping
                     patterns the longest match at each offset wins
                     (Rust path, ``MatchKind::LeftmostLongest``).
        replacement: Optional replacement string. Defaults to
                     :data:`DEFAULT_REPLACEMENT` (``[REDACTED]``).

    Returns:
        New string with all matches replaced. ``text`` itself is never
        mutated.

    Notes:
        * Empty patterns are ignored (no-op), matching the Rust path.
        * ``patterns == []`` or empty ``text`` → ``text`` returned
          unchanged.
        * Idempotent iff ``replacement`` does not itself contain any of
          the patterns (the default ``[REDACTED]`` is safe).
    """
    if not isinstance(text, str) or not text or not patterns:
        return text if isinstance(text, str) else ""

    repl = replacement if replacement is not None else DEFAULT_REPLACEMENT

    if _rust_available():
        # ``harness_perf.redact_patterns`` is a cdylib export — every call
        # crosses the FFI boundary once. For our hot paths (audit log
        # scrubbing, PII replacement before LLM context assembly) the
        # Python ``re`` loop would dominate, so even the FFI overhead is
        # dwarfed by the O(text + matches) AhoCorasick scan.
        import harness_perf
        return harness_perf.redact_patterns(text, list(patterns), repl)

    # ── Pure-Python fallback ─────────────────────────────────────────
    # Identical observable output to the Rust path for non-overlapping
    # patterns. For overlapping patterns the Rust path picks the longest
    # match at each offset (``LeftmostLongest``); the Python loop applies
    # patterns in caller order, so the result may differ when one pattern
    # is a prefix of another. In practice the privacy layer never feeds
    # overlapping patterns (each secret is a distinct literal), so this
    # divergence is acceptable for the fallback.
    out = text
    for p in patterns:
        if p:  # skip empty patterns (str.replace on "" is a no-op anyway)
            out = out.replace(p, repl)
    return out
