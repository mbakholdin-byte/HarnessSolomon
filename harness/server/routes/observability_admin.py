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


# === Outbound webhook DLQ (Phase 4.13B Drift 2) ========================
#
# The DLQ (dead-letter queue) holds outbound deliveries that exhausted
# all retries. Operators list + replay via these endpoints. The
# :class:`~harness.agents.webhook_store.WebhookEventStore` is DI'd via
# ``app.state.webhook_event_store`` (same handle the
# :class:`~harness.agents.outbound.OutboundWebhookDispatcher` uses).
# Replays are gated by ``Scope.WEBHOOK_ADMIN`` (mutation); listing is
# ``Scope.OBSERVABILITY_READ`` (read-only, mirrors the audit endpoint).


def _dlq_entry_to_safe_dict(entry: Any) -> dict[str, Any]:
    """Serialise a :class:`DlqEntry` with PII stripping.

    The payload is the original event dict (already redacted by the
    dispatcher before it was enqueued — see
    :func:`harness.redaction.redact_dict`). We additionally drop any
    preview-style keys that a misconfigured upstream redactor might
    have left in, so the admin surface is defence-in-depth even if
    the dispatcher's redaction pass missed a field.
    """
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    safe_payload = {
        k: v for k, v in payload.items() if k not in _PII_AUDIT_KEYS
    }
    return {
        "id": entry.id,
        "webhook_id": entry.webhook_id,
        "url": entry.url,
        "event_kind": entry.event_kind,
        "payload": safe_payload,
        "last_error": entry.last_error,
        "failed_at": entry.failed_at,
        "replayed_at": entry.replayed_at,
        "attempts": entry.attempts,
    }


def _get_dlq_store(request: Request) -> Any:
    """Pull the :class:`WebhookEventStore` from ``app.state``.

    Returns 503 when the store is not configured (Phase 2.5
    fire-and-forget mode — outbound hardening is disabled).
    """
    store = getattr(request.app.state, "webhook_event_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "webhook_event_store is not configured; outbound DLQ "
                "is unavailable on this server."
            ),
        )
    return store


@router.get("/webhooks/dlq")
async def admin_webhooks_dlq_list(
    request: Request,
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description=(
            "Max DLQ entries to return. Default 100, hard cap 1000. "
            "Only entries that have NOT been replayed are returned "
            "(pass ?include_replayed=true for the full audit history)."
        ),
    ),
    include_replayed: bool = Query(
        default=False,
        description=(
            "When True, also return entries that have been replayed "
            "(audit history). Default False (only pending)."
        ),
    ),
    _token: Any = Depends(require_scope(Scope.OBSERVABILITY_READ)),
) -> dict[str, Any]:
    """List recent outbound webhook DLQ entries (Phase 4.13B Drift 2).

    Returns ``{"entries": [...], "count": N, "limit": limit,
    "include_replayed": bool}``. Each entry is serialised with PII
    stripping (preview fields removed from the payload). The store
    is the single source of truth — there is no in-memory cache.
    """
    store = _get_dlq_store(request)
    entries = await store.list_dlq(
        limit=limit, include_replayed=include_replayed,
    )
    return {
        "entries": [_dlq_entry_to_safe_dict(e) for e in entries],
        "count": len(entries),
        "limit": limit,
        "include_replayed": include_replayed,
    }


@router.post("/webhooks/dlq/{dlq_id}/replay")
async def admin_webhooks_dlq_replay(
    dlq_id: int,
    request: Request,
    _token: Any = Depends(require_scope(Scope.WEBHOOK_ADMIN)),
) -> dict[str, Any]:
    """Replay a single DLQ entry (Phase 4.13B Drift 2).

    Re-sends the original payload to the URL using the CURRENT
    signing secret (honouring ``secret_version`` on the outbound
    config row). On success, the entry is marked ``replayed_at``
    and will not appear in the default list (``?include_replayed=
    false``). On failure, the entry is left untouched (the operator
    can retry).

    Requires ``Scope.WEBHOOK_ADMIN`` (mutation, not just read).

    Returns:
        ``{"dlq_id": id, "replayed": bool, "status_code": int}``.
        ``replayed`` is True when the resend returned 2xx AND the
        store marked the row replayed.
    """
    store = _get_dlq_store(request)
    entry = await store.get_dlq_entry(int(dlq_id))
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"DLQ entry id={dlq_id} not found.",
        )
    if entry.replayed_at is not None:
        # Idempotent: already replayed. Don't resend.
        return {
            "dlq_id": entry.id,
            "replayed": False,
            "status_code": 200,
            "detail": "already replayed",
        }

    # Resolve the current signing secret (honours secret_version).
    import httpx
    from harness.agents.webhook_store import resolve_outbound_secret

    cfg = await store.get_outbound(entry.url)
    secret_version = cfg.secret_version if cfg else 1
    secret = resolve_outbound_secret(secret_version)

    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    # Use the dispatcher's client when available (shares connection
    # pool + timeout config); fall back to a one-shot client.
    dispatcher = getattr(request.app.state, "outbound_dispatcher", None)
    client = (
        getattr(dispatcher, "_client", None)
        if dispatcher is not None
        else None
    )
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        resp = await client.post(
            entry.url, json=entry.payload, headers=headers,
        )
        status_code = resp.status_code
    finally:
        if owns_client and not client.is_closed:
            await client.aclose()

    replayed = False
    if 200 <= status_code < 300:
        replayed = await store.mark_dlq_replayed(entry.id)
        logger.info(
            "observability_admin: dlq replay id=%s url=%s → %d (replayed=%s)",
            entry.id, entry.url, status_code, replayed,
        )
    else:
        logger.warning(
            "observability_admin: dlq replay id=%s url=%s → %d (not marked)",
            entry.id, entry.url, status_code,
        )
    return {
        "dlq_id": entry.id,
        "replayed": replayed,
        "status_code": status_code,
    }


__all__ = ["router"]
