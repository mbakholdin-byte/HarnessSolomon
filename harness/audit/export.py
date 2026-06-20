"""Audit export formatters: CSV and JSON.

Formats a list of dict audit events into CSV or JSON strings.
Uses stdlib only — no external dependencies.

CSV columns (in order):
  - timestamp: ISO 8601 string from the ``ts`` field
  - event_type: canonical event name from the ``event`` field
  - source: derived from ``agent_id`` or empty string
  - message: ``{level} / {status}`` human-readable summary
  - metadata: ``json.dumps(payload)`` serialised payload

Trust boundary: stdlib only. No ``harness.*`` imports needed
(the functions accept plain ``list[dict]``).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any


def _format_ts(ts: float | str) -> str:
    """Convert a LogEvent ``ts`` float to ISO 8601 string."""
    if isinstance(ts, str):
        return ts
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return str(ts)


def _row_from_event(event: dict[str, Any]) -> dict[str, str]:
    """Map a raw audit-event dict to CSV row columns."""
    ts = event.get("ts", "")
    timestamp = _format_ts(ts)
    event_type = str(event.get("event", ""))
    source = str(event.get("agent_id", event.get("session_id", "")))
    level = event.get("level", "")
    status = event.get("status", "")
    message = f"{level} / {status}" if level or status else event_type
    payload = event.get("payload", event.get("aggregate", {}))
    metadata = json.dumps(payload, ensure_ascii=False)
    return {
        "timestamp": timestamp,
        "event_type": event_type,
        "source": source,
        "message": message,
        "metadata": metadata,
    }


_CSV_HEADERS = ("timestamp", "event_type", "source", "message", "metadata")


def to_csv(events: list[dict[str, Any]]) -> str:
    """Format a list of audit events as a CSV string.

    Args:
        events: List of dicts (e.g. LogEvent.to_dict() output).

    Returns:
        CSV-formatted string with ``timestamp,event_type,source,message,metadata``
        header row, followed by one row per event. Properly escapes quotes,
        commas, and newlines via stdlib ``csv`` module (RFC 4180).
    """
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf,
        fieldnames=list(_CSV_HEADERS),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for event in events:
        writer.writerow(_row_from_event(event))
    return buf.getvalue()


def to_json(events: list[dict[str, Any]], indent: int = 2) -> str:
    """Format a list of audit events as a JSON string.

    Args:
        events: List of dicts (e.g. LogEvent.to_dict() output).
        indent: JSON indentation level (default 2).

    Returns:
        Pretty-printed JSON string. ``ensure_ascii=False`` preserves
        Unicode characters.
    """
    return json.dumps(events, ensure_ascii=False, indent=indent, default=str)


__all__ = ["to_csv", "to_json"]
