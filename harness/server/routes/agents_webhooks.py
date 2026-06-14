"""HTTP webhook receiver for inbound GitHub events (Phase 2.3).

Endpoint:
  - ``POST /api/v1/agents/webhooks/github`` — receives
    ``pull_request`` / ``check_run`` / ``pull_request_review``
    events from GitHub, verifies their HMAC, and dispatches
    them to the merge queue's :class:`JobStore`.

Why a separate router (and not folded into ``agents_jobs``):
  - The auth model is different: webhooks use HMAC (shared
    secret in the ``X-Hub-Signature-256`` header), not Bearer
    tokens. We do NOT call ``require_scope`` here.
  - The route is mounted at a configurable path
    (``settings.webhook_path``) so operators can point their
    GitHub repo's webhook settings at it without a code change.
  - The body size cap (``settings.webhook_max_payload_kb``)
    protects against payload-flooding abuse; GitHub's payloads
    are typically <5KB, so 256KB is generous.

Failure modes:
  - 400 on malformed JSON (the handler can't even parse the
    payload to dispatch it).
  - 401 on bad or missing HMAC signature (``X-Hub-Signature-256``
    is required; ``settings.webhook_secret`` must match).
  - 413 on body > ``webhook_max_payload_kb`` (FastAPI's body
    size limit, set via a guard in the handler).
  - 503 when ``webhook_secret`` is empty (webhooks disabled —
    the operator has not configured a secret yet).
  - 200 in all other cases, including redelivery (the
    ``WebhookEventStore`` UNIQUE constraint on ``delivery_id``
    catches duplicates and the handler returns 200 + ``processed: false``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from harness.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


class _WebhookAck(BaseModel):
    """Response shape for a successfully accepted webhook.

    Always returns 200 OK. ``processed`` is True when the
    handler did meaningful work (e.g. marked a job merged);
    False for redeliveries, unknown event types, and
    "we saw this but there's nothing to do" cases.
    """

    delivery_id: str
    event_type: str
    action: str | None = None
    processed: bool = True
    detail: str | None = None


def _get_handler_and_store(request: Request) -> tuple[Any, Any]:
    """Pull the ``WebhookHandler`` and ``WebhookEventStore`` from app state.

    Both are set up in the FastAPI lifespan handler. If either
    is missing (e.g. the lifespan init failed), the route
    returns 503 — the rest of the server (sessions, chat)
    is unaffected, so this is an observably-isolated failure.
    """
    handler = getattr(request.app.state, "webhook_handler", None)
    event_store = getattr(request.app.state, "webhook_event_store", None)
    if handler is None or event_store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "webhook handler not initialised (server lifespan init "
                "failed — is settings.webhook_secret configured?)"
            ),
        )
    return handler, event_store


@router.post(
    "/webhooks/github",
    response_model=_WebhookAck,
    status_code=200,
)
async def receive_github_webhook(request: Request) -> _WebhookAck:
    """Receive a single inbound GitHub webhook event.

    Pipeline (each step short-circuits with the appropriate HTTP code):
      1. Read the raw body (bytes — we need the unparsed body
         for HMAC verification).
      2. Reject if body > ``webhook_max_payload_kb``.
      3. Read ``X-Hub-Signature-256`` / ``X-GitHub-Event`` /
         ``X-GitHub-Delivery`` headers (case-insensitive).
      4. Reject if ``webhook_secret`` is empty (503).
      5. Delegate to ``WebhookHandler.handle_raw`` (HMAC verify
         + idempotency + parse + record). On HMAC fail → 401.
      6. If the event was a redelivery (None from handle_raw),
         return 200 with ``processed: false`` and ``detail`` set.
      7. Otherwise dispatch to the JobStore via
         ``WebhookHandler.dispatch_event``.
      8. Return 200 with the result.

    The route deliberately returns 200 on most "no work to do"
    cases (unknown event types, no matching job, etc.) so
    GitHub doesn't retry — only true error conditions (bad
    signature, malformed JSON, missing secret) return non-200.
    """
    # 1. Read body.
    body = await request.body()
    # 2. Size cap.
    max_bytes = settings.webhook_max_payload_kb * 1024
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"webhook body too large: {len(body)} bytes > "
                f"{max_bytes} bytes (settings.webhook_max_payload_kb)"
            ),
        )
    # 3. Headers (case-insensitive lookup).
    sig = (
        request.headers.get("X-Hub-Signature-256")
        or request.headers.get("x-hub-signature-256")
    )
    event_type = (
        request.headers.get("X-GitHub-Event")
        or request.headers.get("x-github-event")
    )
    delivery_id = (
        request.headers.get("X-GitHub-Delivery")
        or request.headers.get("x-github-delivery")
    )
    if not event_type or not delivery_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "missing required GitHub headers "
                "(X-GitHub-Event, X-GitHub-Delivery)"
            ),
        )
    # 4. Webhooks disabled → 503.
    if not settings.webhook_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "webhooks not configured: set HARNESS_WEBHOOK_SECRET "
                "in the environment to enable the receiver"
            ),
        )
    # 5. Verify + parse + record. ``handle_raw`` raises
    # ``WebhookVerificationError`` on HMAC mismatch (we map to
    # 401) and ``ValueError`` on malformed JSON (we map to 400).
    handler, _ = _get_handler_and_store(request)
    try:
        event = await handler.handle_raw(
            body=body,
            signature=sig,
            event_type=event_type,
            delivery_id=delivery_id,
        )
    except Exception as e:
        # Import the symbol here to avoid coupling the route to the
        # exact class name (the handler's exception types live in
        # ``harness.agents.webhook_handler``).
        from harness.agents.webhook_handler import WebhookVerificationError
        if isinstance(e, WebhookVerificationError):
            # We never echo the signature or the secret in the
            # error message — only the reason code. This avoids
            # leaking material that could help an attacker tune
            # their spoofing attempt.
            status = 401 if e.reason != "missing_secret" else 503
            raise HTTPException(
                status_code=status,
                detail=f"webhook signature check failed: {e.reason}",
            ) from e
        if isinstance(e, ValueError) and "not valid JSON" in str(e):
            raise HTTPException(
                status_code=400,
                detail=str(e),
            ) from e
        # Unknown error — log and 500.
        logger.exception(
            "webhook handler crashed: delivery_id=%s event=%s",
            delivery_id, event_type,
        )
        raise HTTPException(
            status_code=500,
            detail=f"webhook handler error: {type(e).__name__}",
        ) from e
    # 6. Redelivery → no-op.
    if event is None:
        return _WebhookAck(
            delivery_id=delivery_id,
            event_type=event_type,
            action=None,
            processed=False,
            detail="duplicate delivery_id (already processed)",
        )
    # 7. Dispatch to the JobStore.
    job_store = getattr(request.app.state, "job_store", None)
    if job_store is None:
        # The merge-queue JobStore isn't initialised. This
        # happens in dev mode or when the LLM router can't
        # construct the queue. Webhook handler still accepts
        # the event (200) but doesn't update any job.
        return _WebhookAck(
            delivery_id=delivery_id,
            event_type=event_type,
            action=event.action,
            processed=False,
            detail="job_store not initialised; event recorded but not dispatched",
        )
    dispatch_result = await handler.dispatch_event(event, job_store)
    # 8. Return the result.
    return _WebhookAck(
        delivery_id=delivery_id,
        event_type=event_type,
        action=event.action,
        processed=dispatch_result.get("processed", False),
        detail=dispatch_result.get("reason"),
    )


__all__ = ["router"]
