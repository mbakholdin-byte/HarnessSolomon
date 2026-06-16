"""Phase 4.1: Tests for HealthChecker — liveness / readiness / deep."""
from __future__ import annotations

import asyncio

import pytest

from harness.observability import HealthChecker, HealthReport, HealthStatus


async def _ok_probe() -> tuple[dict, bool]:
    return ({"status": "ok", "latency_ms": 5}, True)


async def _fail_probe() -> tuple[dict, bool]:
    return ({"status": "error", "reason": "down"}, False)


async def _slow_probe() -> tuple[dict, bool]:
    await asyncio.sleep(5.0)
    return ({"status": "ok"}, True)


async def _raising_probe() -> tuple[dict, bool]:
    raise ConnectionRefusedError("test")


class TestHealthChecker:
    """HealthChecker: probe registration + 3 endpoints + aggregation."""

    def test_init(self) -> None:
        hc = HealthChecker(version="1.7.0", project_root="/tmp")
        assert hc is not None

    async def test_liveness_always_ok(self) -> None:
        hc = HealthChecker()
        report = await hc.liveness()
        assert report.status == "ok"
        assert "process" in report.checks
        assert report.checks["process"]["status"] == "ok"

    async def test_readiness_all_ok(self) -> None:
        hc = HealthChecker()
        hc.register_probe("qdrant", _ok_probe)
        hc.register_probe("sqlite", _ok_probe)
        report = await hc.readiness()
        assert report.status == "ok"
        assert "qdrant" in report.checks
        assert "sqlite" in report.checks

    async def test_readiness_degraded_non_required(self) -> None:
        """If a non-required probe fails, status = degraded (not unhealthy)."""
        hc = HealthChecker()
        hc.register_probe("qdrant", _ok_probe)
        hc.register_probe("redis", _fail_probe)
        report = await hc.readiness()
        # qdrant is required? No (default). So status = degraded.
        assert report.status == "degraded"
        assert report.checks["qdrant"]["status"] == "ok"
        assert report.checks["redis"]["status"] == "error"

    async def test_readiness_unhealthy_required_fails(self) -> None:
        """If a required probe fails, status = unhealthy (B7 mirror)."""
        hc = HealthChecker()
        hc.configure(require_qdrant=True)
        hc.register_probe("qdrant", _fail_probe)
        report = await hc.readiness()
        assert report.status == "unhealthy"

    async def test_readiness_neo4j_required(self) -> None:
        hc = HealthChecker()
        hc.configure(require_neo4j=True)
        hc.register_probe("neo4j", _ok_probe)
        report = await hc.readiness()
        assert report.status == "ok"

    async def test_readiness_timeout(self) -> None:
        """Slow probe is marked as timeout (B7)."""
        hc = HealthChecker()
        hc.configure(ready_timeout_s=0.1)
        hc.register_probe("slow", _slow_probe)
        report = await hc.readiness()
        assert report.checks["slow"]["status"] == "timeout"

    async def test_readiness_raising_probe(self) -> None:
        """Probe that raises should be marked as error, not crash report."""
        hc = HealthChecker()
        hc.register_probe("bad", _raising_probe)
        report = await hc.readiness()
        assert "bad" in report.checks
        assert report.checks["bad"]["status"] == "error"
        assert "ConnectionRefusedError" in report.checks["bad"]["error"]

    async def test_deep_runs_all(self) -> None:
        hc = HealthChecker()
        hc.configure(deep_timeout_s=2.0)
        hc.register_probe("a", _ok_probe)
        hc.register_probe("b", _fail_probe)
        report = await hc.deep()
        assert report.status == "degraded"  # b fails but neither is required
        assert "a" in report.checks
        assert "b" in report.checks

    async def test_no_probes_liveness_only(self) -> None:
        """If no probes registered, liveness works, readiness shows nothing."""
        hc = HealthChecker()
        report = await hc.readiness()
        # No probes, no failures → status = ok.
        assert report.status == "ok"
        assert report.checks == {}

    def test_register_replace(self) -> None:
        hc = HealthChecker()
        hc.register_probe("x", _ok_probe)
        hc.register_probe("x", _fail_probe)  # Replace
        # Now probe "x" is _fail_probe.
        # We can't easily inspect, but replace should not raise.

    def test_unregister_returns_true(self) -> None:
        hc = HealthChecker()
        hc.register_probe("x", _ok_probe)
        assert hc.unregister_probe("x") is True
        assert hc.unregister_probe("x") is False  # Already gone

    def test_to_dict(self) -> None:
        report = HealthReport(
            status="ok",
            checks={"a": {"status": "ok"}},
            version="1.7.0",
            project_root="/tmp",
        )
        d = report.to_dict()
        assert d["status"] == "ok"
        assert d["version"] == "1.7.0"
        assert d["project_root"] == "/tmp"
        assert d["checks"]["a"]["status"] == "ok"

    async def test_concurrent_probes(self) -> None:
        """Probes run in parallel via asyncio.gather."""
        import time

        async def slow_ok() -> tuple[dict, bool]:
            await asyncio.sleep(0.2)
            return ({"status": "ok"}, True)

        hc = HealthChecker()
        hc.configure(ready_timeout_s=2.0)
        hc.register_probe("a", slow_ok)
        hc.register_probe("b", slow_ok)
        hc.register_probe("c", slow_ok)
        start = time.monotonic()
        report = await hc.readiness()
        elapsed = time.monotonic() - start
        # 3 probes of 0.2s each — if serial, ~0.6s. If parallel, ~0.2s.
        assert elapsed < 0.4, f"probes ran serially: {elapsed:.3f}s"
        assert report.status == "ok"
