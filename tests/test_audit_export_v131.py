"""WI-03: Audit Export — tests for date range filter + CSV/JSON export.

Covers ``harness/server/routes/audit.py``:
  - test_date_filter_returns_events_in_range
  - test_export_csv_format
  - test_export_json_format
  - test_pagination_limit_offset
  - test_invalid_format_returns_400
  - test_empty_result_returns_empty_list
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.observability.events import LogEvent
from harness.observability.logger import JsonlLogger
from harness.server.app import create_app


# === Helpers ===


def _make_log_event(
    event: str = "test_event",
    ts: float | None = None,
    *,
    payload: dict[str, Any] | None = None,
    level: str = "INFO",
    status: str = "ok",
    session_id: str = "",
    agent_id: str = "",
) -> LogEvent:
    """Build a synthetic LogEvent for seeding the JSONL log."""
    import time as _time
    return LogEvent(
        event=event,
        payload=payload or {"test": True},
        level=level,  # type: ignore[arg-type]
        session_id=session_id,
        agent_id=agent_id,
        ts=ts if ts is not None else _time.time(),
        status=status,  # type: ignore[arg-type]
    )


def _dt_ts(dt: datetime) -> float:
    """Convert a datetime to a float timestamp."""
    return dt.timestamp()


@pytest.fixture
def seeded_log_dir(
    isolated_settings: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Create an isolated JSONL log dir seeded with test events.

    Returns the log dir path (also monkeypatched into settings).
    """
    log_dir = isolated_settings["data"] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "observability_log_dir", log_dir)

    logger = JsonlLogger(log_dir)

    # Seed events at known timestamps
    t1 = _dt_ts(datetime(2026, 6, 19, 10, 0, 0, tzinfo=timezone.utc))
    t2 = _dt_ts(datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc))
    t3 = _dt_ts(datetime(2026, 6, 20, 8, 0, 0, tzinfo=timezone.utc))
    t4 = _dt_ts(datetime(2026, 6, 20, 14, 0, 0, tzinfo=timezone.utc))
    t5 = _dt_ts(datetime(2026, 6, 21, 16, 0, 0, tzinfo=timezone.utc))

    events = [
        _make_log_event("evt_a", ts=t1, status="ok"),
        _make_log_event("evt_b", ts=t2, status="error"),
        _make_log_event("evt_c", ts=t3, status="ok"),
        _make_log_event("evt_d", ts=t4, status="timeout"),
        _make_log_event("evt_e", ts=t5, status="cancelled"),
    ]

    for evt in events:
        logger.emit(evt)

    return log_dir


@pytest.fixture
async def audit_app(
    isolated_settings: dict[str, Path],
    seeded_log_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Create a fresh FastAPI app with the seeded log dir wired.

    Auth is in open dev mode by default (conftest sets
    ``auth_required=False``). The app's router includes the
    audit routes.
    """
    app = create_app()
    return app


@pytest.fixture
async def audit_client(audit_app: Any) -> AsyncClient:
    """Async HTTP client bound to the audit app."""
    from asgi_lifespan import LifespanManager

    async with LifespanManager(audit_app):
        transport = ASGITransport(app=audit_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# === Tests ===


class TestDateFilter:
    """Date-range filter: ``?from=...&to=...``."""

    async def test_date_filter_returns_events_in_range(
        self,
        audit_client: AsyncClient,
    ) -> None:
        """Only events within the inclusive ``from``/``to`` range are returned."""
        r = await audit_client.get(
            "/api/v1/audit",
            params={
                "from": "2026-06-20T00:00:00Z",
                "to": "2026-06-20T23:59:59Z",
                "limit": "100",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Should have exactly 2 events: evt_c (T08:00) and evt_d (T14:00)
        event_names = [e["event"] for e in body]
        assert len(body) == 2, f"Expected 2 events, got {len(body)}: {event_names}"
        assert "evt_c" in event_names
        assert "evt_d" in event_names

    async def test_empty_result_returns_empty_list(
        self,
        audit_client: AsyncClient,
    ) -> None:
        """A date range with no matching events returns 200 with empty list."""
        r = await audit_client.get(
            "/api/v1/audit",
            params={
                "from": "2020-01-01T00:00:00Z",
                "to": "2020-01-01T23:59:59Z",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == []


class TestExportFormat:
    """Format selection: ``?format=json`` and ``?format=csv``."""

    async def test_export_json_format(
        self,
        audit_client: AsyncClient,
    ) -> None:
        """``?format=json`` returns ``application/json`` content type."""
        r = await audit_client.get(
            "/api/v1/audit",
            params={"format": "json", "limit": "5"},
        )
        assert r.status_code == 200, r.text
        ct = r.headers.get("content-type", "")
        assert "application/json" in ct
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 5  # all 5 seeded events

    async def test_export_csv_format(
        self,
        audit_client: AsyncClient,
    ) -> None:
        """``?format=csv`` returns ``text/csv`` with proper CSV structure."""
        r = await audit_client.get(
            "/api/v1/audit",
            params={"format": "csv", "limit": "5"},
        )
        assert r.status_code == 200, r.text
        ct = r.headers.get("content-type", "")
        assert "text/csv" in ct
        body = r.text
        # Verify CSV header row
        lines = body.strip().split("\n")
        assert len(lines) >= 2, f"Expected header + at least 1 row, got:\n{body}"
        header = lines[0]
        assert "timestamp" in header
        assert "event_type" in header
        assert "source" in header
        assert "message" in header
        assert "metadata" in header
        # Verify at least one data row
        assert len(lines) >= 2  # header + data rows


class TestPagination:
    """Pagination: ``?limit=...&offset=...``."""

    async def test_pagination_limit_offset(
        self,
        audit_client: AsyncClient,
    ) -> None:
        """Limit and offset paginate correctly."""
        # Page 1: offset=0, limit=2 → first 2 (newest: evt_e, evt_d)
        r1 = await audit_client.get(
            "/api/v1/audit",
            params={"limit": "2", "offset": "0"},
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert len(body1) == 2, f"Expected 2, got {len(body1)}"
        assert r1.headers.get("X-Total-Count") == "5"

        # Page 2: offset=2, limit=2 → next 2
        r2 = await audit_client.get(
            "/api/v1/audit",
            params={"limit": "2", "offset": "2"},
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert len(body2) == 2

        # Page 3: offset=4, limit=2 → last 1
        r3 = await audit_client.get(
            "/api/v1/audit",
            params={"limit": "2", "offset": "4"},
        )
        assert r3.status_code == 200, r3.text
        body3 = r3.json()
        assert len(body3) == 1

        # No overlap: pages should be disjoint
        names1 = {e["event"] for e in body1}
        names2 = {e["event"] for e in body2}
        names3 = {e["event"] for e in body3}
        assert names1.isdisjoint(names2)
        assert names1.isdisjoint(names3)
        assert names2.isdisjoint(names3)


class TestValidation:
    """Input validation."""

    async def test_invalid_format_returns_400(
        self,
        audit_client: AsyncClient,
    ) -> None:
        """An unsupported ``?format=`` value returns HTTP 400."""
        r = await audit_client.get(
            "/api/v1/audit",
            params={"format": "xml"},
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "xml" in detail.lower() or "format" in detail.lower()
