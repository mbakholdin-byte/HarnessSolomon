"""Phase 4.13B v1.23.0: Outbound webhook admin endpoints.

Operator-facing surface for the auto-disable circuit breaker
introduced in Phase 4.13B Drift 1. Provides:

  * ``POST /api/v1/webhooks/{url}/enable`` — re-enable an
    auto-disabled outbound URL (resets the failure counter and
    clears ``disabled_at``). Requires ``Scope.WEBHOOK_ADMIN``.

The DLQ listing + replay endpoints live in
:mod:`harness.server.routes.observability_admin` (mounted under
``/api/v1/observability/webhooks/dlq``) because they share the
observability read surface and the ``OBSERVABILITY_READ`` scope.
This split mirrors the existing pattern: lifecycle mutations
(``webhooks.admin``) vs. read-only introspection
(``observability.read``).

RBAC
----

All endpoints require ``Scope.WEBHOOK_ADMIN``. In open dev mode
(``settings.auth_required=False``) the scope check is bypassed
(mirrors the Phase 1.6 dependency semantics).

Trust boundary
--------------

This module imports only from stdlib (``logging``, ``urllib.parse``),
FastAPI, :mod:`harness.config`, and :mod:`harness.server.auth`. It
does NOT import from :mod:`harness.agents` — the
:class:`~harness.agents.webhook_store.WebhookEventStore` is DI'd via
``app.state.webhook_event_store`` so the boundary is preserved at
the AST level (mirrors the existing
:mod:`harness.server.routes.observability_admin` pattern).
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
import fastapi

from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks-admin"])


def _get_event_store(request: Request) -> Any:
    """Pull the :class:`WebhookEventStore` from ``app.state``.

    Set up by the FastAPI lifespan handler. If missing (e.g. the
    outbound dispatcher is running without a store — Phase 2.5
    fire-and-forget mode), the endpoint returns 503 so the operator
    knows the admin surface is unavailable, rather than silently
    accepting a no-op.
    """
    store = getattr(request.app.state, "webhook_event_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "webhook_event_store is not configured on this server "
                "(outbound delivery hardening is disabled). Set up the "
                "store in the lifespan handler to enable the admin surface."
            ),
        )
    return store


def _decode_url_path_segment(encoded: str) -> str:
    """URL-decode a path segment that carries a webhook URL.

    Outbound URLs contain slashes and colons (``https://host/path``),
    which can't appear raw in a FastAPI path param. Operators pass
    the URL percent-encoded; we decode it here so the store lookup
    matches the value the dispatcher recorded. We do NOT decode
    twice (defence in depth against double-encoding bugs).
    """
    return urllib.parse.unquote(encoded)


@router.post("/webhooks/enable")
async def enable_webhook(
    request: Request,
    url: str = fastapi.Query(
        ...,
        description=(
            "The outbound webhook URL to re-enable. Outbound URLs "
            "contain slashes and colons (``https://host/path``) "
            "which can't appear raw in a path segment, so the URL "
            "is passed as a query parameter (``?url=<value>``). "
            "Percent-encoding is optional — Starlette decodes query "
            "params once."
        ),
    ),
    _token: Any = Depends(require_scope(Scope.WEBHOOK_ADMIN)),
) -> dict[str, Any]:
    """Re-enable an auto-disabled outbound webhook URL.

    Resets ``disabled_at`` to NULL and ``consecutive_failures`` to 0.
    The next delivery attempt will go through normally; if the
    endpoint is still flaky, the counter climbs again and the URL
    is re-disabled at the threshold.

    Query param:
        ``url``: The outbound URL to re-enable. Pass raw (the query
        string handles slashes natively) or percent-encoded.

    Returns:
        ``{"url": <url>, "enabled": <bool>}`` where
        ``enabled`` is True if the row was previously disabled and
        is now active, False if it was already active (idempotent
        no-op). A 404 is returned when the URL has never been
        recorded by the dispatcher (no config row exists).
    """
    store = _get_event_store(request)
    # Check existence first so we can 404 cleanly (vs. enable_outbound
    # silently returning False for both "already active" and "unknown").
    cfg = await store.get_outbound(url)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"outbound webhook url={url!r} not found. The dispatcher "
                f"creates a config row on first delivery; this URL has "
                f"never been attempted."
            ),
        )
    enabled = await store.enable_outbound(url)
    logger.info(
        "webhooks_admin: enable url=%s enabled=%s (was_disabled=%s)",
        url, enabled, cfg.disabled_at is not None,
    )
    return {"url": url, "enabled": enabled}


__all__ = ["router"]
