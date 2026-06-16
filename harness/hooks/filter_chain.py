"""Phase 4.0: match_glob-style filter for hook context fields.

A filter chain is a comma-separated list of ``<field>=<pattern>``
rules. A hook fires only if EVERY rule matches (AND semantics).

Supported fields (Phase 4.0):
    - ``event`` (EventType.value)
    - ``session_id``
    - ``agent_id``
    - ``tool_name`` (from PreToolUse / PostToolUse payload)
    - ``request_id``
    - ``payload.<key>`` (e.g. ``payload.agent_name``)

Pattern syntax (light glob):
    - ``*``  matches any chars (including ``/``)
    - ``!``  prefix negates (does NOT match)
    - literal match otherwise

Empty filter chain = matches everything.

This module is stdlib only (no harness imports).
"""
from __future__ import annotations

import fnmatch
from typing import Any


def _match_pattern(value: str, pattern: str) -> bool:
    """Match ``value`` against a glob pattern.

    ``!pattern`` = negation. Other chars = fnmatch (case-sensitive).
    Empty pattern = matches everything.
    """
    if not pattern:
        return True
    if pattern.startswith("!"):
        return not fnmatch.fnmatchcase(value, pattern[1:])
    return fnmatch.fnmatchcase(value, pattern)


def _resolve_field(context_dict: dict[str, Any], field: str) -> str:
    """Resolve a field path like ``payload.tool_name`` to a string value.

    Returns ``""`` for missing paths.
    """
    cur: Any = context_dict
    for part in field.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return ""
        if cur is None:
            return ""
    return str(cur) if cur is not None else ""


def parse_filter_chain(spec: str) -> list[tuple[str, str]]:
    """Parse ``"field=pattern,field=pattern"`` into ``[(field, pattern), ...]``.

    Whitespace is stripped. Empty spec = empty list (matches all).
    """
    if not spec:
        return []
    out: list[tuple[str, str]] = []
    for raw in spec.split(","):
        rule = raw.strip()
        if not rule:
            continue
        if "=" not in rule:
            # Treat as bare event-name filter for backwards compat.
            out.append(("event", rule))
            continue
        field, _, pattern = rule.partition("=")
        out.append((field.strip(), pattern.strip()))
    return out


def matches_filter_chain(
    spec: str,
    *,
    event: str,
    session_id: str,
    agent_id: str,
    payload: dict[str, Any],
    request_id: str = "",
) -> bool:
    """Return True if a context matches the filter chain (AND semantics)."""
    rules = parse_filter_chain(spec)
    if not rules:
        return True
    fields = {
        "event": event,
        "session_id": session_id,
        "agent_id": agent_id,
        "request_id": request_id,
        "tool_name": str(payload.get("tool_name", "")),
    }
    for field, pattern in rules:
        if field.startswith("payload."):
            value = _resolve_field(payload, field[len("payload.") :])
        else:
            value = fields.get(field, "")
        if not _match_pattern(value, pattern):
            return False
    return True


__all__ = [
    "parse_filter_chain",
    "matches_filter_chain",
    "_match_pattern",
    "_resolve_field",
]
