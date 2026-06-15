"""Phase 3: redaction regex patterns.

Twelve categories, all stdlib ``re`` (no third-party deps). Each pattern is
case-insensitive where appropriate. Patterns are designed to be high-precision
(false positives are worse than misses for a privacy tool — see CLAUDE.md §
"Seбесurity" and the risk register in the Phase 3 plan).

Idempotency guarantee: every replacement placeholder (``<EMAIL>``, etc.)
contains no recognisable secret, so re-running ``redact()`` cannot double-match.
"""
from __future__ import annotations

import re

# Email — standard pattern, lowercase localpart + domain.
PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL": re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    ),
    # Phone — E.164-ish: optional +, 7+ digits with optional separators.
    "PHONE": re.compile(
        r"(?<!\d)\+?\d[\d\s\-()]{7,}\d(?!\d)",
    ),
    # IPv4 — four 0-255 octets with word boundaries.
    "IPV4": re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b",
    ),
    # GitHub PAT — six variants (classic, fine-grained, OAuth, user, server,
    # refresh). Classic (ghp_) is the most common leak.
    "GITHUB_TOKEN": re.compile(
        r"\b(?:ghp_|github_pat_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9_]{36,82}\b",
    ),
    # AWS access key — starts with AKIA / ASIA, 16 uppercase alnum.
    "AWS_ACCESS_KEY": re.compile(
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    ),
    # AWS secret heuristic — `aws_secret_access_key=...` with 40-char base64-ish.
    "AWS_SECRET": re.compile(
        r"(?i)\baws_secret_access_key\b\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})",
    ),
    # OpenAI / Anthropic — `sk-...` (OpenAI) or `sk-ant-...` (Anthropic).
    # Note: char class includes `-` to allow `sk-proj-...` (project-scoped
    # OpenAI keys, which are the modern default). Length 20+ is the same
    # as upstream's heuristic.
    "OPENAI_KEY": re.compile(
        r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_])",
    ),
    "ANTHROPIC_KEY": re.compile(
        r"(?<![A-Za-z0-9_])sk-ant-[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_])",
    ),
    # Generic .env assignment — `secret=...`, `password=...`, etc. followed by
    # a long value. Heuristic: any of the known keyword names + non-whitespace
    # value of length >= 8. Allow word boundary OR underscore separator so
    # ``DB_PASSWORD=hunter2`` matches (the underscore is a word char, so
    # ``\b`` alone doesn't anchor cleanly here).
    "ENV_ASSIGNMENT": re.compile(
        r"(?i)(?:\b|_)(?:secret|password|passwd|pwd|api_key|apikey|"
        r"access_key|token|private_key|client_secret)"
        r"\s*[:=]\s*['\"]?([^\s'\"<>]{8,})",
    ),
    # JWT — header.payload.signature with base64-ish segments. Three segments
    # separated by dots, each at least 10 chars. Common leak in .env files.
    "JWT": re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\."
        r"[A-Za-z0-9_\-]{10,}\b",
    ),
    # PEM private key block — header line is the giveaway.
    "PEM_PRIVATE_KEY": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
    ),
    # Slack token — xoxb / xoxa / xoxp / xoxr / xoxs prefixes.
    "SLACK_TOKEN": re.compile(
        r"\bxox[baprs]-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{24,}\b",
    ),
}


def placeholder(category: str) -> str:
    """Return the replacement placeholder for a category.

    Angle brackets make the placeholder visually obvious in logs and to the
    LLM (which can use the category to reason about the redacted content).
    """
    return f"<{category}>"
