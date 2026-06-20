"""WI-03: Audit log endpoints — date-range filter + CSV/JSON export.

REST endpoint:
  ``GET /api/v1/audit`` — list audit entries with date-range filter,
  pagination, and format selection.

Query parameters:
  * ``from`` (ISO 8601, optional) — inclusive lower bound on ``ts`` field
  * ``to`` (ISO 8601, optional) — inclusive upper bound on ``ts`` field
  * ``format`` (``json`` | ``csv``, default ``json``) — response format
  * ``limit`` (int, default 100, max 1000) — page size
  * ``offset`` (int, default 0) — pagination offset

RBAC: ``Scope.OBSERVABILITY_READ`` required. In open dev mode
(``auth_required=False``) the check is bypassed (mirrors the
existing Phase 1.6 dependency semantics).

Data source: reads ``harness-YYYY-MM-DD.jsonl`` files from
``settings.observability_log_dir``. Events are parsed,
date-filtered, paginated, and formatted on the fly.

Trust boundary: stdlib + FastAPI + ``harness.config`` +
``harness.audit`` + ``harness.server.auth``. Does NOT import
from ``harness.agents`` or ``harness.hooks`` directly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response

from harness.audit import to_csv, to_json
from harness.config import settings
from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])

# Maximum allowed limit — mirror the
# ``hooks_observability_admin_audit_max_limit`` pattern (1000 is generous
# but safe for a single response payload).
_MAX_LIMIT = 1000


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to a timezone-aware ``datetime``.

    Returns None for empty/missing input. Raises ``ValueError`` on
    unparseable input (caught by the route → HTTP 400).
    """
    if value is None or value.strip() == "":
        return None
    # Accept both "Z" suffix and "+HH:MM" offsets.
    s = value.strip()
    # Python 3.11+ supports "Z" in fromisoformat, but we normalise
    # for 3.12+ compatibility.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    # Ensure timezone-aware for consistent comparison
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_ts(d: dict[str, Any]) -> float:
    """Extract the ``ts`` field from an event dict as a float.

    The ``ts`` field is a ``time.time()`` float (seconds since epoch).
    Missing or unparseable → returns 0.0 (very old, effectively a no-op
    for date filtering).
    """
    raw = d.get("ts")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _read_events(
    log_dir: Path,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Read audit events from JSONL files in ``log_dir``.

    Files are scanned from newest to oldest (by filename). Within each
    file, lines are read in order. The date filter is applied AFTER
    parsing (post-filter), so the pagination window is applied to the
    filtered set.

    Returns:
        ``(events, total_matched)`` — ``events`` is the slice
        ``[offset : offset + limit]`` of the filtered results;
        ``total_matched`` is the total count of events matching the
        date filter (for pagination metadata).
    """
    from_dt = date_from
    to_dt = date_to
    from_ts = from_dt.timestamp() if from_dt else None
    to_ts = to_dt.timestamp() if to_dt else None

    # Collect matching file paths, sorted newest first
    if not log_dir.exists():
        return [], 0
    files = sorted(
        log_dir.glob("harness-*.jsonl"),
        key=lambda p: p.name,
        reverse=True,  # newest first
    )

    # Narrow file list by date range (filename contains YYYY-MM-DD)
    if from_dt or to_dt:
        narrowed: list[Path] = []
        for fp in files:
            # Extract date part from filename: harness-YYYY-MM-DD.jsonl
            stem = fp.stem  # e.g. "harness-2026-06-20"
            date_str = stem[len("harness-"):]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                narrowed.append(fp)  # keep files with non-standard names
                continue
            if from_dt and file_date < from_dt.replace(
                hour=0, minute=0, second=0, microsecond=0
            ):
                continue  # file is entirely before the range
            if to_dt and file_date > to_dt.replace(
                hour=23, minute=59, second=59, microsecond=999999
            ):
                continue  # file is entirely after the range
            narrowed.append(fp)
        files = narrowed

    matched: list[dict[str, Any]] = []

    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Date filter (inclusive range on ts)
            ts = _event_ts(evt)
            if from_ts is not None and ts < from_ts:
                continue
            if to_ts is not None and ts > to_ts:
                continue

            matched.append(evt)

    total_matched = len(matched)

    # Sort newest first for audit consumption
    matched.sort(key=_event_ts, reverse=True)

    # Paginate
    paginated = matched[offset : offset + limit]

    return paginated, total_matched


@router.get("")
async def audit_list(
    request: Request,
    date_from: str | None = Query(
        default=None,
        alias="from",
        description="Inclusive lower bound (ISO 8601), e.g. 2026-06-19T00:00:00Z",
    ),
    date_to: str | None = Query(
        default=None,
        alias="to",
        description="Inclusive upper bound (ISO 8601), e.g. 2026-06-20T23:59:59Z",
    ),
    fmt: str = Query(
        default="json",
        alias="format",
        description="Response format: 'json' (application/json) or 'csv' (text/csv)",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Page size (1–1000, default 100)",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Pagination offset (0-based)",
    ),
    _token: Any = Depends(require_scope(Scope.OBSERVABILITY_READ)),
) -> Response:
    """List audit entries with date-range filter, pagination, and format.

    Returns:
      * ``format=json`` → ``application/json``: list of event dicts
      * ``format=csv`` → ``text/csv; charset=utf-8``: CSV document
      * Empty result → 200 with ``[]`` (JSON) or headers-only CSV
    """
    # Validate format
    fmt = fmt.strip().lower()
    if fmt not in ("json", "csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format: {fmt!r}. Use 'json' or 'csv'.",
        )

    # Parse date filters
    try:
        parsed_from = _parse_iso8601(date_from)
        parsed_to = _parse_iso8601(date_to)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date format: {exc}. Use ISO 8601 (e.g. 2026-06-19T00:00:00Z).",
        ) from exc

    # Read events from JSONL log directory
    log_dir = settings.observability_log_dir
    events, total = _read_events(
        log_dir,
        date_from=parsed_from,
        date_to=parsed_to,
        limit=limit,
        offset=offset,
    )

    # Build response
    if fmt == "csv":
        csv_body = to_csv(events)
        return PlainTextResponse(
            content=csv_body,
            media_type="text/csv; charset=utf-8",
            headers={
                "X-Total-Count": str(total),
                "X-Limit": str(limit),
                "X-Offset": str(offset),
            },
        )

    # JSON
    return Response(
        content=to_json(events),
        media_type="application/json; charset=utf-8",
        headers={
            "X-Total-Count": str(total),
            "X-Limit": str(limit),
            "X-Offset": str(offset),
        },
    )


__all__ = ["router"]
