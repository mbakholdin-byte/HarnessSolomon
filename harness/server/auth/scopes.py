"""Phase 1.6 — Scope definitions and matching helpers.

Scopes are dot-separated lowercase strings. The closed set
(``ALL_SCOPES``) is the union of every scope the API currently
recognises; new scopes are added by extending the ``Scope`` enum
and providing a description in ``SCOPE_DESCRIPTIONS``.

Matching semantics
------------------
``has_scope(token_scopes, required)`` uses **ANY** match (logical OR).
A token with scopes ``{memory.read, sessions.read}`` can call any
endpoint whose required set intersects with those two values. We do
NOT require all required scopes — that would force callers to send
"the kitchen sink" on every request, which is the wrong default for
a scope-gated API at this scale. If a future endpoint needs strict
ALL-of matching, we'll add a separate ``has_all_scopes()`` helper
rather than change the semantics of the existing one.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterable


class Scope(str, Enum):
    """The closed set of scopes the Phase 1.6 API recognises.

    String values match the wire format (``Authorization: Bearer X``
    + ``GET /api/v1/capabilities`` returns these strings). The
    ``str`` mixin means ``Scope.AGENTS_READ == "agents.read"`` is True,
    so callers can compare against strings directly if they want.
    """

    # Sub-agent system (Phase 2.x)
    AGENTS_READ = "agents.read"
    AGENTS_WRITE = "agents.write"
    AGENTS_PR = "agents.pr"  # Phase 2.3+ — declared but no routes yet

    # 4-layer memory (Phase 1+)
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"

    # Session metadata (read-only for now)
    SESSIONS_READ = "sessions.read"
    # Phase 3 v1.4.0: write session control operations (manual /compact)
    SESSIONS_WRITE = "sessions.write"


ALL_SCOPES: frozenset[Scope] = frozenset(Scope)


SCOPE_DESCRIPTIONS: dict[Scope, str] = {
    Scope.AGENTS_READ: "Read sub-agent jobs and queue stats (GET /api/v1/agents/jobs*)",
    Scope.AGENTS_WRITE: "Enqueue / cancel sub-agent jobs (POST /api/v1/agents/jobs)",
    Scope.AGENTS_PR: "Open and merge GitHub PRs via the merge queue (Phase 2.3+)",
    Scope.MEMORY_READ: "Search the 4-layer memory system (GET /api/v1/memory/*)",
    Scope.MEMORY_WRITE: "Dual-write notes to the 4-layer memory system (POST /api/v1/memory/notes)",
    Scope.SESSIONS_READ: "Read session metadata (GET /api/v1/sessions)",
    Scope.SESSIONS_WRITE: "Force-compact a session's context (POST /api/v1/sessions/{id}/compact, Phase 3 v1.4.0)",
}


def scope_description(scope: Scope) -> str:
    """Human-readable description for the capabilities endpoint.

    Falls back to a generic string for unknown scopes (defence in
    depth — we never expect to see one, but a future enum value
    without an entry in ``SCOPE_DESCRIPTIONS`` would otherwise crash
    the capabilities endpoint).
    """
    return SCOPE_DESCRIPTIONS.get(scope, "(no description)")


def parse_scopes(text: str) -> set[Scope]:
    """Parse a comma-separated scope string into a set of ``Scope``.

    Accepts whitespace around the commas and is case-insensitive on
    the input side (the enum canonicalises to lowercase). Unknown
    scope names raise ``ValueError`` so misconfigured ``--scopes``
    on the CLI is caught immediately rather than silently producing
    a token with zero effective permissions.

    Examples::

        parse_scopes("agents.read, memory.write")
            → {Scope.AGENTS_READ, Scope.MEMORY_WRITE}

        parse_scopes("AGENTS.READ")
            → {Scope.AGENTS_READ}

        parse_scopes("agents.read, foo.bar")
            → ValueError: unknown scope: foo.bar
    """
    if not text or not text.strip():
        return set()
    scopes: set[Scope] = set()
    for raw in text.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        try:
            scopes.add(Scope(name))
        except ValueError as exc:
            valid = ", ".join(s.value for s in Scope)
            raise ValueError(
                f"unknown scope: {name!r} (valid scopes: {valid})"
            ) from exc
    return scopes


def format_scopes(scopes: Iterable[Scope]) -> str:
    """Inverse of :func:`parse_scopes` — used in CLI output and logs.

    Joins the canonical wire-format values with ``", "``. The
    special token ``*`` is returned for the ALL_SCOPES case so
    that ``harness auth list`` doesn't flood the screen when the
    bootstrap admin is in play.
    """
    scope_set = set(scopes)
    if scope_set == ALL_SCOPES:
        return "*"
    return ", ".join(sorted(s.value for s in scope_set))


def has_scope(token_scopes: Iterable[Scope], required: Iterable[Scope]) -> bool:
    """Return True iff the token has at least one of the required scopes.

    Empty ``required`` is treated as 'no requirement' (returns True)
    so that ``require_scope()`` with no args is a no-op dependency —
    useful for routes that are always public but still want to
    participate in the auth pipeline (e.g. the capabilities endpoint
    itself, which is always public but uses the same dependency
    mechanism for consistency).
    """
    required_set = set(required)
    if not required_set:
        return True
    return any(s in token_scopes for s in required_set)


__all__ = [
    "ALL_SCOPES",
    "SCOPE_DESCRIPTIONS",
    "Scope",
    "format_scopes",
    "has_scope",
    "parse_scopes",
    "scope_description",
]
