"""Phase 3: redaction engine.

Pure functions. No I/O, no logging, no settings reads. The caller decides
where to log matches (``RedactionAudit`` is one option).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from harness.redaction.patterns import PATTERNS, placeholder


@dataclass(frozen=True)
class RedactionMatch:
    """A single match of a redaction pattern in source text.

    ``original`` is the matched substring. **Never** log this in production
    code — the audit log records category and count only.
    """

    category: str
    start: int
    end: int
    original: str


def scan(text: str, *, categories: set[str] | None = None) -> list[RedactionMatch]:
    """Find all redaction matches in ``text`` without mutating it.

    Args:
        text:       Source string. Empty / non-str → empty list.
        categories: If set, only run these patterns. None = run all
                    12 defaults.

    Returns:
        List of ``RedactionMatch`` (start, end, original). Order is
        determined by the underlying ``re.finditer`` (left-to-right by
        start position). Overlapping matches: ``re.finditer`` returns
        non-overlapping matches per pattern; patterns are run in
        ``PATTERNS`` insertion order.
    """
    if not isinstance(text, str) or not text:
        return []
    selected = (
        {k: v for k, v in PATTERNS.items() if k in categories}
        if categories is not None
        else PATTERNS
    )
    matches: list[RedactionMatch] = []
    for category, pattern in selected.items():
        for m in pattern.finditer(text):
            matches.append(
                RedactionMatch(
                    category=category,
                    start=m.start(),
                    end=m.end(),
                    original=m.group(0),
                )
            )
    return matches


def redact(text: str, *, categories: set[str] | None = None) -> str:
    """Replace all redaction matches in ``text`` with category placeholders.

    Idempotent: ``redact(redact(x)) == redact(x)``. Placeholders contain
    no recognisable secrets, so re-running cannot match them again.
    Non-str input → returned unchanged (defensive — sinks should not crash
    on None / int / dict).

    Args:
        text:       Source string.
        categories: If set, only run these patterns. None = all defaults.

    Returns:
        New string with matches replaced. ``text`` itself is not mutated.
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    selected = (
        {k: v for k, v in PATTERNS.items() if k in categories}
        if categories is not None
        else PATTERNS
    )
    out = text
    for category, pattern in selected.items():
        out = pattern.sub(placeholder(category), out)
    return out


def redact_dict(
    d: dict,
    fields: set[str],
    *,
    categories: set[str] | None = None,
) -> dict:
    """Recursively redact string values in selected fields.

    Walks the dict, list, and nested-dict structure. For each leaf key
    in ``fields`` (matched by exact key name, any depth), the value is
    redacted if it's a string. Non-string values are left untouched.

    Returns a new dict; ``d`` is not mutated.
    """
    if not isinstance(d, dict):
        return d

    def _walk(node: object) -> object:
        if isinstance(node, dict):
            return {
                k: (
                    redact(v, categories=categories)
                    if isinstance(v, str) and k in fields
                    else _walk(v)
                )
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(d)  # type: ignore[return-value]
