"""Phase 4.11 Task B v1.21.0: Admin observability endpoints (JSON, RBAC-gated).

Companion to :mod:`harness.server.routes.observability` (Phase 4.1), which
exposes the public Prometheus-text ``/metrics`` and ``/health/*`` probes.
This module adds the **admin** surface — structured JSON responses intended
for operator dashboards (Grafana JSON panels, custom alerting, audit
review tooling) — gated behind ``Scope.OBSERVABILITY_READ``.

Endpoints
---------
  * ``GET /api/v1/observability/metrics`` — JSON snapshot of all Prometheus
    counters + gauges (reuses :meth:`PrometheusMetrics.snapshot`).
    Optional ``?filter=<regex>`` narrows the returned metric names.
  * ``GET /api/v1/observability/health/deep`` — JSON deep health report
    (reuses :meth:`HealthChecker.deep`, 8 subsystem probes from Phase
    4.9 Task C).
  * ``GET /api/v1/observability/audit/recent?limit=N`` — recent
    :class:`HookAuditSink` entries (last N, default 50, max
    ``settings.hooks_observability_admin_audit_max_limit``).

RBAC
----
All three endpoints require ``Scope.OBSERVABILITY_READ``. In open dev mode
(``settings.auth_required=False``) the scope check is bypassed (mirrors
the Phase 1.6 dependency semantics).

Settings
--------
  * ``hooks_observability_admin_enabled`` (default True) — when False,
    the router is not mounted by :mod:`harness.server.app` and the
    endpoints return 404.
  * ``hooks_observability_admin_audit_max_limit`` (default 500) — upper
    bound on the ``limit`` query parameter.
  * ``hooks_observability_admin_metrics_filter`` (default "") — server-wide
    regex filter on metric names (overridable per-request via ``?filter=``).

PII safety
----------
The metrics snapshot, health probes, and audit tail are all read from
observability primitives that already redact PII upstream (the
:mod:`harness.redaction` engine runs before any hook payload is persisted
to the audit sink). The admin endpoints additionally strip any
``question_preview`` / ``arguments_preview`` keys from audit entries
before returning them, so even a misconfigured upstream redactor cannot
leak user prompts or tool arguments through this surface.

Trust boundary
--------------
This module imports only from stdlib (``logging``, ``re``, ``json``),
FastAPI, :mod:`harness.config`, :mod:`harness.observability`, and
:mod:`harness.server.auth`. It does NOT import from :mod:`harness.agents`
or :mod:`harness.hooks` directly — the audit sink and health checker are
duck-typed via ``app.state`` lookups so the boundary is preserved at the
AST level (mirrors the existing
:mod:`harness.server.routes.observability` pattern).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from harness.config import settings
from harness.observability import get_observability
from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["observability-admin"])


# === PII safety helpers ===

# Keys stripped from every audit entry before it leaves the admin surface.
# These mirror the upstream redaction engine's "preview" fields — if the
# upstream engine missed a value (e.g. an exotic format), the admin
# endpoint still removes the key so the value cannot leak via this path.
_PII_AUDIT_KEYS: frozenset[str] = frozenset(
    {
        "question_preview",
        "arguments_preview",
        "prompt_preview",
        "answer",
        "raw_payload",
    }
)


def _strip_pii(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``entry`` with PII-bearing keys removed.

    Recurses one level into the ``aggregate`` sub-dict (the audit entry
    shape is ``{ts, event, session_id, agent_id, request_id, aggregate}``
    where ``aggregate`` is the hook decision payload).
    """
    out = {k: v for k, v in entry.items() if k not in _PII_AUDIT_KEYS}
    agg = out.get("aggregate")
    if isinstance(agg, dict):
        out["aggregate"] = {
            k: v for k, v in agg.items() if k not in _PII_AUDIT_KEYS
        }
    return out


def _apply_metrics_filter(
    snapshot: dict[str, Any],
    pattern: str,
) -> dict[str, Any]:
    """Filter the metrics snapshot by metric name using ``pattern`` (regex).

    Empty ``pattern`` returns the snapshot unchanged. An invalid regex
    is treated as a literal substring match (defensive — never raise
    from inside a route handler over a filter typo).
    """
    if not pattern:
        return snapshot
    try:
        compiled = re.compile(pattern)
        return {
            name: series
            for name, series in snapshot.items()
            if compiled.search(name)
        }
    except re.error:
        logger.warning(
            "observability_admin: invalid metrics filter %r — treating as literal",
            pattern,
        )
        return {
            name: series
            for name, series in snapshot.items()
            if pattern in name
        }


# === Endpoints ===


@router.get("/metrics")
async def admin_metrics(
    request: Request,
    filter: str | None = Query(
        default=None,
        description=(
            "Optional regex filter on metric names. When set, only "
            "metrics whose name matches the pattern are returned. "
            "Overrides ``settings.hooks_observability_admin_metrics_filter`` "
            "for this request."
        ),
    ),
    _token: Any = Depends(require_scope(Scope.OBSERVABILITY_READ)),
) -> dict[str, Any]:
    """Return a JSON snapshot of all Prometheus counters + gauges.

    Same data as ``GET /metrics`` (Prometheus text format), but as
    nested JSON for easier consumption by admin tools. Histograms are
    excluded (the Prometheus text format is their canonical export).
    """
    obs = get_observability()
    snapshot = obs.metrics.snapshot()
    # Per-request filter overrides the server-wide setting; fall back
    # to the setting only when the caller did not pass ``?filter=``.
    effective_filter = (
        filter if filter is not None else settings.hooks_observability_admin_metrics_filter
    )
    return _apply_metrics_filter(snapshot, effective_filter or "")


@router.get("/health/deep")
async def admin_health_deep(
    request: Request,
    _token: Any = Depends(require_scope(Scope.OBSERVABILITY_READ)),
) -> dict[str, Any]:
    """Return a JSON deep health report (8 subsystem probes).

    Reuses :meth:`HealthChecker.deep` from the singleton observability
    handle. The response shape is the canonical ``HealthReport.to_dict()``
    output (``{status, version, project_root, checks, probes, ts}``).
    """
    obs = get_observability()
    report = await obs.health.deep()
    return report.to_dict()


@router.get("/audit/recent")
async def admin_audit_recent(
    request: Request,
    limit: int = Query(
        default=50,
        ge=1,
        description=(
            "Number of recent audit entries to return. Capped at "
            "``settings.hooks_observability_admin_audit_max_limit`` "
            "(default 500)."
        ),
    ),
    _token: Any = Depends(require_scope(Scope.OBSERVABILITY_READ)),
) -> list[dict[str, Any]]:
    """Return the last N :class:`HookAuditSink` entries.

    The audit sink is DI'd via ``app.state.audit_sink`` by the FastAPI
    lifespan handler. When the sink is not configured (no
    ``hooks_audit_log=True``), the endpoint returns an empty list
    rather than 503 — the absence of audit logging is a valid state
    and the admin tool should see ``[]`` (not an error).
    """
    max_limit = settings.hooks_observability_admin_audit_max_limit
    if limit > max_limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"limit ({limit}) exceeds maximum allowed "
                f"({max_limit}); set hooks_observability_admin_audit_max_limit "
                f"to raise the cap"
            ),
        )
    sink = getattr(request.app.state, "audit_sink", None)
    if sink is None:
        return []
    # HookAuditSink exposes ``tail(n)`` (the canonical method name).
    # ``read_recent`` is the handoff's aspirational name; we keep the
    # real method here and let the lifespan wire the same sink.
    entries: list[dict[str, Any]] = sink.tail(limit)
    # PII safety: strip preview fields before serialising to JSON.
    return [_strip_pii(entry) for entry in entries]


__all__ = ["router"]
