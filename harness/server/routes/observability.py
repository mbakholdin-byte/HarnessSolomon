"""Phase 4.1 Step 6.11: Observability HTTP routes.

Exposes Prometheus metrics + health probes:

  - ``GET /metrics`` → Prometheus text format (no-op if disabled).
  - ``GET /health/live`` → liveness probe (no deps, always fast).
  - ``GET /health/ready`` → readiness probe (configurable probes).
  - ``GET /health/deep`` → deep probe (sum of all registered probes).
  - ``GET /api/health`` → backward-compat alias for ``/health/deep``.

All routes are public (no scope required) — monitoring stacks must
not need an API token to scrape.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from harness.observability import get_observability

router = APIRouter(tags=["observability"])


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint. Disabled by default in Settings."""
    obs = get_observability()
    body = obs.metrics.render()
    media_type = obs.metrics.content_type
    return Response(content=body, media_type=media_type)


@router.get("/health/live")
async def health_live() -> dict:
    """Liveness: is the process alive? Always 200 unless the Python
    interpreter is broken. Used by Kubernetes liveness probe.
    """
    obs = get_observability()
    report = await obs.health.liveness()
    return report.to_dict()


@router.get("/health/ready")
async def health_ready(response: Response) -> dict:
    """Readiness: are the required dependencies up? Returns 503 if
    ``require_qdrant`` / ``require_neo4j`` are set and the dep is down.
    """
    obs = get_observability()
    report = await obs.health.readiness()
    if report.status == "unhealthy":
        response.status_code = 503
    return report.to_dict()


@router.get("/health/deep")
async def health_deep(response: Response) -> dict:
    """Deep health: run every registered probe with full timeout.
    Used by ops dashboards.
    """
    obs = get_observability()
    report = await obs.health.deep()
    if report.status == "unhealthy":
        response.status_code = 503
    return report.to_dict()


@router.get("/api/health")
async def health_alias() -> dict:
    """Backward-compat alias for ``/health/deep`` (Phase 0+)."""
    obs = get_observability()
    report = await obs.health.deep()
    return report.to_dict()
