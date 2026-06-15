"""Solomon Harness — pre-LLM PII / secret redaction (Phase 3).

Public API:
    - ``redact(text, *, categories=None) -> str`` — replace matches with
      category-labeled placeholders.
    - ``scan(text) -> list[RedactionMatch]`` — return all matches with offsets
      (no mutation). Used by ``RedactionAudit`` to record the audit log.
    - ``redact_dict(d, fields) -> dict`` — recursive redaction of selected
      string-valued keys in a dict.
    - ``PATTERNS: dict[str, re.Pattern]`` — 12+ stdlib regex patterns.
    - ``RedactionMatch`` — dataclass holding category, start, end, original.

Design notes:
    - Idempotent: ``redact(redact(x)) == redact(x)``. Placeholders contain no
      recognisable secrets so re-running cannot double-match.
    - Defense in depth: ``PrivacyAwareEmbedder`` (memory/embeddings/privacy.py)
      runs redact() BEFORE embedding, so vectors never see PII.
    - Pure: no I/O, no logging, no settings reads. The caller (``RedactionAudit``
      + the 9 sink-point wrappers) decides where to log.
"""
from __future__ import annotations

from harness.redaction.engine import (
    RedactionMatch,
    redact,
    redact_dict,
    scan,
)
from harness.redaction.patterns import PATTERNS

__all__ = [
    "PATTERNS",
    "RedactionMatch",
    "redact",
    "redact_dict",
    "scan",
]
