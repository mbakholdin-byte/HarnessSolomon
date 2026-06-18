"""Phase 4.9 Task C: Deep health probes — 8 subsystem tests.

Verifies that ``HealthChecker.deep()`` correctly:
  - runs all 8 subsystem probes in parallel
  - treats ``db`` / ``job_store`` as critical (failure → ``unhealthy``)
  - treats the other 6 probes as non-critical (failure → ``degraded``)
  - skips optional probes when their config is None
  - wraps each probe in a 2s timeout
  - exposes version + timestamp in the report
  - integrates with the FastAPI ``/health/deep`` endpoint
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from harness.observability import HealthChecker, ProbeResult


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FakeJobStore:
    """Minimal async JobStore stub: ``count_jobs()`` returns an int."""

    def __init__(self, count: int = 0) -> None:
        self._count = count

    async def count_jobs(self) -> int:
        return self._count


class FailingJobStore:
    """JobStore stub that raises on ``count_jobs()``."""

    async def count_jobs(self) -> int:
        raise RuntimeError("simulated job_store failure")


class SlowJobStore:
    """JobStore stub that sleeps past the 2s probe timeout."""

    async def count_jobs(self) -> int:
        await asyncio.sleep(5.0)
        return 0


class FakeStats:
    """Stub for MergeQueue / ElicitationBroker — exposes ``stats()``."""

    def __init__(self, *, payload: dict | None = None, raise_exc: BaseException | None = None) -> None:
        self._payload = payload if payload is not None else {"pending": 0}
        self._raise = raise_exc

    def stats(self) -> dict:
        if self._raise is not None:
            raise self._raise
        return self._payload


class AsyncStats:
    """Variant of FakeStats where ``stats()`` returns a coroutine."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def stats(self) -> dict:
        return self._payload


class FakeRateLimiter:
    """Stub for HookRateLimiter — sync ``check()`` returning bool."""

    def __init__(self, ok: bool = True) -> None:
        self._ok = ok

    def check(self) -> bool:
        return self._ok


class AsyncRateLimiter:
    """Stub: ``check()`` returns a coroutine."""

    def __init__(self, ok: bool = True) -> None:
        self._ok = ok

    async def check(self) -> bool:
        return self._ok


class RaisingRateLimiter:
    def check(self) -> bool:
        raise RuntimeError("rate limiter down")


def _make_db(tmp_path: Path) -> Path:
    """Create a valid SQLite DB that responds to ``SELECT 1``."""
    # Ensure the parent directory exists (sqlite3.connect will fail
    # with OperationalError if the parent is missing).
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    db = tmp_path if tmp_path.suffix == ".db" else tmp_path / "agent-jobs.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS _meta (k TEXT)")
    conn.commit()
    conn.close()
    return db


def _full_checker(
    *,
    db_path: Any | None = None,
    job_store: Any | None = None,
    merge_queue: Any | None = None,
    elicitation_broker: Any | None = None,
    notify_channels: list[str] | None = None,
    rate_limiter: Any | None = None,
    qdrant_url: str | None = None,
    opensearch_url: str | None = None,
    version: str = "1.18.0",
) -> HealthChecker:
    """Build a HealthChecker with the given subsystem kwargs.

    All kwargs are threaded through explicitly so callers can pass
    ``None`` to test the skipped-probe path or ``[]`` (empty list)
    for ``notify_channels`` to test the configured-but-empty path.
    """
    return HealthChecker(
        version=version,
        db_path=db_path,
        job_store=job_store,
        merge_queue=merge_queue,
        elicitation_broker=elicitation_broker,
        notify_channels=notify_channels,
        rate_limiter=rate_limiter,
        qdrant_url=qdrant_url,
        opensearch_url=opensearch_url,
    )


# ---------------------------------------------------------------------------
# 1. All probes ok
# ---------------------------------------------------------------------------


async def test_deep_all_probes_ok(tmp_path: Path) -> None:
    """All 8 probes pass → overall status ``ok``."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(count=3),
        merge_queue=FakeStats(payload={"pending": 1, "running": 0}),
        elicitation_broker=AsyncStats(payload={"open": 0}),
        notify_channels=["slack", "teams"],
        rate_limiter=FakeRateLimiter(ok=True),
    )
    report = await hc.deep()
    assert report.status == "ok"
    assert set(report.probes.keys()) == {
        "db",
        "qdrant",
        "opensearch",
        "job_store",
        "merge_queue",
        "elicitation_broker",
        "notify_channels",
        "rate_limiter",
    }
    # Critical probes must be ok.
    assert report.probes["db"].status == "ok"
    assert report.probes["job_store"].status == "ok"
    # Optional probes not configured → skipped (still counts as ok aggregate).
    assert report.probes["qdrant"].status == "skipped"
    assert report.probes["opensearch"].status == "skipped"
    # The configured optional probes report ok.
    assert report.probes["merge_queue"].status == "ok"
    assert report.probes["elicitation_broker"].status == "ok"
    assert report.probes["notify_channels"].status == "ok"
    assert report.probes["rate_limiter"].status == "ok"


# ---------------------------------------------------------------------------
# 2. Critical probe failures
# ---------------------------------------------------------------------------


async def test_deep_db_failure_makes_critical(tmp_path: Path) -> None:
    """Broken DB file → db probe fails → overall ``unhealthy``.

    We point db_path at a DIRECTORY path — ``sqlite3.connect`` raises
    ``OperationalError: unable to open database file`` when the path
    is a directory (it can't read/write a directory as a file).
    """
    bad_path = tmp_path  # the tmp_path directory itself
    hc = _full_checker(
        db_path=bad_path,
        job_store=FakeJobStore(),
    )
    report = await hc.deep()
    assert report.status == "unhealthy"
    assert report.probes["db"].status == "down"
    assert report.probes["job_store"].status == "ok"


async def test_deep_job_store_failure_makes_critical(tmp_path: Path) -> None:
    """JobStore raises on count_jobs() → job_store probe fails → ``unhealthy``."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FailingJobStore(),
    )
    report = await hc.deep()
    assert report.status == "unhealthy"
    assert report.probes["db"].status == "ok"
    assert report.probes["job_store"].status == "down"
    assert "simulated job_store failure" in report.probes["job_store"].message


# ---------------------------------------------------------------------------
# 3. Non-critical probe failures → degraded
# ---------------------------------------------------------------------------


async def test_deep_qdrant_failure_makes_degraded(tmp_path: Path) -> None:
    """Qdrant unreachable → degraded (non-critical)."""
    db = _make_db(tmp_path)
    # Use a port that's almost certainly closed. 127.0.0.1:1 is
    # typically refused immediately.
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        qdrant_url="http://127.0.0.1:1",
    )
    report = await hc.deep()
    assert report.status == "degraded"
    assert report.probes["db"].status == "ok"
    assert report.probes["job_store"].status == "ok"
    assert report.probes["qdrant"].status == "down"


async def test_deep_opensearch_failure_makes_degraded(tmp_path: Path) -> None:
    """OpenSearch unreachable → degraded (non-critical)."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        opensearch_url="http://127.0.0.1:1",
    )
    report = await hc.deep()
    assert report.status == "degraded"
    assert report.probes["opensearch"].status == "down"


async def test_deep_merge_queue_failure_makes_degraded(tmp_path: Path) -> None:
    """MergeQueue.stats() raises → degraded (non-critical)."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        merge_queue=FakeStats(raise_exc=RuntimeError("mq down")),
    )
    report = await hc.deep()
    assert report.status == "degraded"
    assert report.probes["merge_queue"].status == "down"
    assert "mq down" in report.probes["merge_queue"].message


async def test_deep_elicitation_broker_failure_makes_degraded(tmp_path: Path) -> None:
    """ElicitationBroker.stats() raises → degraded (non-critical)."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        elicitation_broker=FakeStats(raise_exc=ValueError("broker broken")),
    )
    report = await hc.deep()
    assert report.status == "degraded"
    assert report.probes["elicitation_broker"].status == "down"


# ---------------------------------------------------------------------------
# 4. Specific probe behaviour
# ---------------------------------------------------------------------------


async def test_deep_notify_channels_check_config(tmp_path: Path) -> None:
    """Empty channels list is treated as ok (no channels to check)."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        notify_channels=[],
    )
    report = await hc.deep()
    # Empty list is NOT None → probe runs → reports ok with 0 channels.
    assert report.probes["notify_channels"].status == "ok"
    assert "0 channel" in report.probes["notify_channels"].message


async def test_deep_notify_channels_rejects_bad_entries(tmp_path: Path) -> None:
    """Non-string / empty-string channel entries → probe reports down."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        notify_channels=["ok", "", 123],  # type: ignore[list-item]
    )
    report = await hc.deep()
    assert report.probes["notify_channels"].status == "down"
    assert report.status == "degraded"  # non-critical


async def test_deep_rate_limiter_probe(tmp_path: Path) -> None:
    """Sync RateLimiter.check() returning True → probe ok."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        rate_limiter=FakeRateLimiter(ok=True),
    )
    report = await hc.deep()
    assert report.probes["rate_limiter"].status == "ok"

    # Async variant should also work.
    hc2 = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        rate_limiter=AsyncRateLimiter(ok=True),
    )
    report2 = await hc2.deep()
    assert report2.probes["rate_limiter"].status == "ok"

    # Raising limiter → degraded (non-critical).
    hc3 = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        rate_limiter=RaisingRateLimiter(),
    )
    report3 = await hc3.deep()
    assert report3.probes["rate_limiter"].status == "down"
    assert report3.status == "degraded"


# ---------------------------------------------------------------------------
# 5. Timeout + parallelism
# ---------------------------------------------------------------------------


async def test_deep_probe_timeout_handled(tmp_path: Path) -> None:
    """Probe slower than 2s is reported as ``timeout`` and aggregated as failure."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=SlowJobStore(),  # 5s sleep > 2s timeout
    )
    start = time.monotonic()
    report = await hc.deep()
    elapsed = time.monotonic() - start
    # Critical job_store timed out → unhealthy. The 2s timeout bounds
    # total wall time: should be ~2s, definitely < 4s.
    assert elapsed < 4.0, f"deep() took too long: {elapsed:.2f}s"
    assert report.status == "unhealthy"
    assert report.probes["job_store"].status == "timeout"
    assert "timed out" in report.probes["job_store"].message


async def test_deep_probes_run_in_parallel(tmp_path: Path) -> None:
    """All probes run concurrently — total latency ≈ max, not sum."""
    db = _make_db(tmp_path)

    class SlowStats:
        def __init__(self) -> None:
            self.delay = 0.3

        async def stats(self) -> dict:
            await asyncio.sleep(self.delay)
            return {"ok": True}

    class SlowLimiter:
        async def check(self) -> bool:
            await asyncio.sleep(0.3)
            return True

    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        merge_queue=SlowStats(),
        elicitation_broker=SlowStats(),
        rate_limiter=SlowLimiter(),
    )
    start = time.monotonic()
    report = await hc.deep()
    elapsed = time.monotonic() - start
    # Three 0.3s probes in parallel → ~0.3s, definitely < 0.7s.
    # If serial → ~0.9s.
    assert elapsed < 0.7, f"probes ran serially: {elapsed:.3f}s"
    assert report.status == "ok"


# ---------------------------------------------------------------------------
# 6. Optional probe skip
# ---------------------------------------------------------------------------


async def test_deep_optional_probe_skipped_when_not_configured(tmp_path: Path) -> None:
    """Probes with None config report status ``skipped`` (not failure)."""
    db = _make_db(tmp_path)
    hc = _full_checker(
        db_path=db,
        job_store=FakeJobStore(),
        # qdrant_url, opensearch_url, merge_queue, etc. all None
    )
    report = await hc.deep()
    assert report.status == "ok"
    assert report.probes["qdrant"].status == "skipped"
    assert report.probes["opensearch"].status == "skipped"
    assert report.probes["merge_queue"].status == "skipped"
    assert report.probes["elicitation_broker"].status == "skipped"
    assert report.probes["notify_channels"].status == "skipped"
    assert report.probes["rate_limiter"].status == "skipped"


# ---------------------------------------------------------------------------
# 7. Report metadata
# ---------------------------------------------------------------------------


async def test_deep_version_in_report(tmp_path: Path) -> None:
    """``version`` field in the deep report matches the constructor arg."""
    db = _make_db(tmp_path)
    hc = _full_checker(db_path=db, job_store=FakeJobStore(), version="9.9.9")
    report = await hc.deep()
    assert report.version == "9.9.9"
    d = report.to_dict()
    assert d["version"] == "9.9.9"
    # ``ts`` is populated for the subsystem deep() path.
    assert report.ts != ""
    assert d["ts"] == report.ts
    assert d["ts"].endswith("Z")


# ---------------------------------------------------------------------------
# 8. FastAPI /health/deep integration
# ---------------------------------------------------------------------------


@pytest.fixture
async def deep_app(isolated_settings, monkeypatch):
    """FastAPI app whose HealthChecker is wired with subsystem kwargs.

    We patch the ``get_observability`` accessor so ``/health/deep``
    hits our test-configured HealthChecker. ``isolated_settings``
    ensures paths are isolated; we point ``db_path`` at a real
    (empty) SQLite file so the ``db`` probe succeeds.
    """
    from harness.observability import (
        ObservabilityHandle,
        get_observability,
        reset_observability,
    )
    from harness.config import settings

    # Create a real SQLite DB file so the db probe succeeds.
    db = isolated_settings["data"] / "agent-jobs.db"
    db = _make_db(db)
    job_store = FakeJobStore(count=2)
    health = HealthChecker(
        version="1.18.0",
        db_path=db,
        job_store=job_store,
    )
    # Build a fresh handle (this also caches the singleton; we'll
    # override the accessor below so routes see our patched health).
    reset_observability()
    base = get_observability(settings)
    new_handle = ObservabilityHandle(
        settings=base.settings,
        logger=base.logger,
        metrics=base.metrics,
        tracer=base.tracer,
        health=health,
        cost=base.cost,
    )

    # Patch ``get_observability`` itself (the function) so every call
    # returns our patched handle. The route module imports the symbol
    # via ``from harness.observability import get_observability`` so
    # the binding is captured in the route module's namespace; we
    # patch it there (and also in the two source modules for safety).
    for modpath in (
        "harness.observability.emit.get_observability",
        "harness.observability.get_observability",
        "harness.server.routes.observability.get_observability",
    ):
        monkeypatch.setattr(modpath, lambda *a, **kw: new_handle)

    from harness.server.app import create_app

    app = create_app()
    yield app
    reset_observability()


async def test_deep_health_endpoint_returns_200(deep_app) -> None:
    """``GET /health/deep`` returns 200 when all critical probes pass."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=deep_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health/deep")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")  # db+job_store ok → ok
    assert "probes" in body
    assert "db" in body["probes"]
    assert body["probes"]["db"]["status"] == "ok"
    assert body["probes"]["job_store"]["status"] == "ok"


async def test_deep_health_endpoint_returns_503_on_down(isolated_settings, monkeypatch) -> None:
    """``GET /health/deep`` returns 503 when a CRITICAL probe fails."""
    from harness.observability import (
        ObservabilityHandle,
        get_observability,
        reset_observability,
    )
    from harness.config import settings

    # Point db_path at an EXISTING directory → sqlite3.connect fails
    # with OperationalError (can't open a directory as a DB file).
    # NB: ``isolated_settings["data"]`` may not exist yet, in which
    # case sqlite3 would silently create a *file* at that path. We
    # explicitly mkdir() it to force the directory-vs-file mismatch.
    bad_db_dir = isolated_settings["data"]
    bad_db_dir.mkdir(parents=True, exist_ok=True)
    health = HealthChecker(
        version="1.18.0",
        db_path=bad_db_dir,  # existing directory → sqlite3 error
        job_store=FailingJobStore(),
    )
    reset_observability()
    base = get_observability(settings)
    new_handle = ObservabilityHandle(
        settings=base.settings,
        logger=base.logger,
        metrics=base.metrics,
        tracer=base.tracer,
        health=health,
        cost=base.cost,
    )
    # Patch the accessor in all three places it can be imported from
    # (the emit module, the package __init__ re-export, and the route
    # module's early-bound import).
    for modpath in (
        "harness.observability.emit.get_observability",
        "harness.observability.get_observability",
        "harness.server.routes.observability.get_observability",
    ):
        monkeypatch.setattr(modpath, lambda *a, **kw: new_handle)

    from harness.server.app import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health/deep")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["probes"]["db"]["status"] == "down"


# ---------------------------------------------------------------------------
# 9. Backward-compat: legacy deep() path still works
# ---------------------------------------------------------------------------


async def test_deep_legacy_path_when_no_subsystem_config() -> None:
    """When NO subsystem kwargs are configured, deep() falls back to
    the legacy registry-based path (Phase 4.1 backward-compat).
    """
    hc = HealthChecker(version="1.18.0")

    async def _ok_probe() -> tuple[dict, bool]:
        return ({"status": "ok"}, True)

    async def _fail_probe() -> tuple[dict, bool]:
        return ({"status": "error"}, False)

    hc.register_probe("legacy_a", _ok_probe)
    hc.register_probe("legacy_b", _fail_probe)
    report = await hc.deep()
    # Legacy path: non-required probe fails → degraded.
    assert report.status == "degraded"
    assert "legacy_a" in report.checks
    assert "legacy_b" in report.checks
    # probes dict should be empty in legacy path (no subsystem probes ran).
    assert report.probes == {}


# ---------------------------------------------------------------------------
# 10. ProbeResult dataclass sanity
# ---------------------------------------------------------------------------


def test_probe_result_to_dict_roundtrip() -> None:
    """ProbeResult.to_dict() includes status, latency_ms, message."""
    pr = ProbeResult(status="ok", latency_ms=12.3456, message="hello")
    d = pr.to_dict()
    assert d["status"] == "ok"
    # latency_ms is rounded to 3 decimals.
    assert d["latency_ms"] == 12.346
    assert d["message"] == "hello"
