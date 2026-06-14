"""GitHub webhook handler — Phase 2.3.

This module is the entry point for inbound GitHub webhook events.
It does three things:

  1. **Verify the HMAC-SHA256 signature** (the
     ``X-Hub-Signature-256`` header). We do this BEFORE doing
     any other work (no JSON parse, no DB write) so a flood of
     spoofed events is cheap to reject.
  2. **Parse the payload** into a normalised :class:`WebhookEvent`
     Pydantic model. The 3 event types we care about (Phase 2.3)
     are ``pull_request``, ``check_run``, ``pull_request_review``;
     everything else is logged and ignored (the route still
     returns 200 so GitHub doesn't retry).
  3. **Idempotency** — the ``delivery_id`` from
     ``X-GitHub-Delivery`` is recorded in :class:`WebhookEventStore`
     with a UNIQUE constraint; redeliveries return 200 and skip
     processing.

This module is **inbound** — it does NOT talk to GitHub (no ``gh``
calls). The HMAC secret is set by the operator (``settings.webhook_secret``)
and shared with GitHub's webhook configuration page. Tokens from
the Phase 1.6 token store are NOT used here (webhook auth is
inbound, tokens are outbound).

Trust boundary
--------------

This module imports only stdlib (hashlib, hmac, json, logging) +
Pydantic + :mod:`harness.agents.webhook_store` + (for ``dispatch_event``)
:mod:`harness.agents.jobs`. It does NOT import from
:mod:`harness.server` — Phase 2.0's boundary is preserved. The
HTTP route lives in :mod:`harness.server.routes.agents_webhooks`,
which is the ONLY place that bridges this module into the FastAPI
app.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.agents.jobs import JobStore
from harness.agents.webhook_store import WebhookEventStore

logger = logging.getLogger(__name__)


# === Errors ===

class WebhookVerificationError(Exception):
    """Raised when an inbound webhook's HMAC signature is invalid.

    The ``reason`` attribute is a short string suitable for logging
    and for mapping to HTTP status codes:
      - ``"missing_signature"`` → 401 (the header is missing or empty)
      - ``"bad_signature"`` → 401 (the HMAC didn't match)
      - ``"missing_secret"`` → 503 (the server has no secret configured;
        webhooks are disabled in this state — the route should 503)
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"webhook verification failed: {reason}")
        self.reason: str = reason


# === HMAC verification ===

def _expected_signature(body: bytes, secret: str) -> str:
    """Compute the expected ``sha256=...`` signature for ``body``.

    Returns the hex digest prefixed with ``sha256=`` (the format
    GitHub uses in the ``X-Hub-Signature-256`` header). The hash
    function is HMAC-SHA256, NOT plain SHA-256 (the difference is
    that HMAC uses the secret as the key, preventing
    length-extension attacks).
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


def verify_github_signature(
    *,
    body: bytes,
    signature_header: str | None,
    secret: str,
) -> None:
    """Verify the ``X-Hub-Signature-256`` header against the body.

    Uses :func:`hmac.compare_digest` for timing-safe comparison
    (so an attacker can't measure how many bytes of the signature
    are correct). Raises :class:`WebhookVerificationError` on
    mismatch; returns ``None`` on success.

    Args:
        body:              The raw request body (bytes, not str).
        signature_header:  The value of ``X-Hub-Signature-256``
                           (e.g. ``"sha256=abc123..."``). Case is
                           normalised (GitHub always sends lowercase
                           ``sha256=`` but we don't rely on it).
        secret:            The shared HMAC secret. If empty, the
                           caller should treat this as "webhooks
                           disabled" and 503 — we raise
                           ``missing_secret`` to signal that.

    Raises:
        WebhookVerificationError: ``reason="missing_secret"`` if
            the server has no secret configured;
            ``reason="missing_signature"`` if the header is
            missing or empty; ``reason="bad_signature"`` if the
            HMAC didn't match.
    """
    if not secret:
        raise WebhookVerificationError("missing_secret")
    if not signature_header or not signature_header.strip():
        raise WebhookVerificationError("missing_signature")
    # Normalise: strip whitespace, lowercase the scheme prefix.
    sig = signature_header.strip()
    # GitHub sends ``sha256=...`` (lowercase). Some clients send
    # ``SHA256=...``; we accept both by lowercasing the scheme.
    if "=" in sig:
        scheme, _, value = sig.partition("=")
        sig = f"{scheme.lower()}={value}"
    expected = _expected_signature(body, secret)
    if not hmac.compare_digest(expected, sig):
        raise WebhookVerificationError("bad_signature")
    return None


# === Pydantic model ===

class WebhookEvent(BaseModel):
    """Normalised view of an inbound GitHub webhook event.

    Only the fields we care about are exposed. The full raw payload
    is also stored in :class:`WebhookEventStore` for debugging, but
    the dispatch logic uses this model.

    For events we don't recognise (anything other than the 3
    types Phase 2.3 handles), the parser still produces a
    :class:`WebhookEvent` with ``event_type`` set and the
    actionable fields (``pr_number`` / ``conclusion`` / etc.) as
    ``None``. The dispatcher treats those as "log and ignore".
    """

    delivery_id: str
    event_type: str
    action: str | None = None
    #: PR number (int). Extracted from ``pull_request.number``,
    #: ``check_run.pull_requests[0].number``, or
    #: ``pull_request_review.pull_request.number``. ``None`` for
    #: event types that don't carry a PR reference.
    pr_number: int | None = None
    #: PR HTML URL (str). Same sources as ``pr_number``. ``None``
    #: if the event has no PR context.
    pr_url: str | None = None
    #: Commit SHA the event is about (e.g. ``check_run.head_sha``).
    #: Used by the dispatcher to correlate check_run events with
    #: a specific SHA the queue pushed. ``None`` if not present.
    head_sha: str | None = None
    #: Check-run conclusion (``"success"`` / ``"failure"`` / etc.).
    #: Only populated for ``check_run`` events.
    conclusion: str | None = None
    #: Review state (``"approved"`` / ``"changes_requested"`` / etc.).
    #: Only populated for ``pull_request_review`` events.
    review_state: str | None = None
    #: True for ``pull_request`` events with ``action="closed"``
    #: AND ``pull_request.merged == true``. The dispatcher uses
    #: this to transition ``pr_auto_merge_enabled`` jobs to
    #: ``merged``.
    pr_merged: bool = False


# === Parsing ===

def parse_github_payload(
    event_type: str,
    payload: dict[str, Any],
) -> WebhookEvent:
    """Normalise a raw GitHub webhook payload into a :class:`WebhookEvent`.

    Dispatches on ``event_type``:

    - ``"pull_request"`` — reads ``action`` and the nested
      ``pull_request.{html_url, head.sha, merged}`` fields. The
      PR number is at the TOP level of the payload (not nested
      under ``pull_request``).
    - ``"check_run"`` — reads ``action``, the nested
      ``check_run.{head_sha, conclusion}`` and
      ``check_run.pull_requests[0].{number, html_url}`` (GitHub
      attaches the linked PRs to the check_run).
    - ``"pull_request_review"`` — reads ``action``, the nested
      ``review.state`` and ``pull_request.{number, html_url}``.
    - any other ``event_type`` — returns a :class:`WebhookEvent`
      with only ``event_type`` and ``delivery_id`` populated.
      The dispatcher logs + ignores these.

    Args:
        event_type:  The ``X-GitHub-Event`` header value.
        payload:     The parsed JSON body as a dict.

    Returns:
        A :class:`WebhookEvent` with the normalised fields.
    """
    # Common fields: action is at the top level of every GitHub event
    # that has one. ``delivery_id`` is filled in by the caller (it
    # comes from a header, not the payload), so we use a placeholder
    # here and overwrite it in ``WebhookHandler.handle_raw``.
    action = payload.get("action")

    if event_type == "pull_request":
        pr = payload.get("pull_request") or {}
        head = pr.get("head") or {}
        return WebhookEvent(
            delivery_id="",  # filled by handle_raw
            event_type=event_type,
            action=action,
            pr_number=payload.get("number"),
            pr_url=pr.get("html_url"),
            head_sha=head.get("sha"),
            pr_merged=bool(pr.get("merged", False)),
        )

    if event_type == "check_run":
        cr = payload.get("check_run") or {}
        # GitHub attaches an array of linked PRs to a check_run. We
        # use the FIRST one (in practice, a check_run is typically
        # associated with one PR; this is the standard pattern).
        linked_prs = cr.get("pull_requests") or []
        first_pr = linked_prs[0] if linked_prs else {}
        return WebhookEvent(
            delivery_id="",
            event_type=event_type,
            action=action,
            pr_number=first_pr.get("number"),
            pr_url=first_pr.get("html_url"),
            head_sha=cr.get("head_sha"),
            conclusion=cr.get("conclusion"),
        )

    if event_type == "pull_request_review":
        review = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        return WebhookEvent(
            delivery_id="",
            event_type=event_type,
            action=action,
            pr_number=pr.get("number") or payload.get("number"),
            pr_url=pr.get("html_url"),
            review_state=review.get("state"),
        )

    # Unknown event type — return a minimal event. The dispatcher
    # will log + ignore it; the row is still recorded in
    # ``webhook_events`` for ops auditability.
    return WebhookEvent(
        delivery_id="",
        event_type=event_type,
        action=action,
    )


# === Handler ===

class WebhookHandler:
    """Top-level orchestrator for inbound webhook events.

    Owns the :class:`WebhookEventStore` (for idempotency) and the
    secret (for HMAC verification). The route in
    :mod:`harness.server.routes.agents_webhooks` constructs one of
    these at startup and delegates every inbound event to
    :meth:`handle_raw`.

    Args:
        store:  The :class:`WebhookEventStore` to record events in.
        secret: The HMAC-SHA256 shared secret. If empty, ``handle_raw``
                will raise :class:`WebhookVerificationError` with
                ``reason="missing_secret"`` (the route should map
                this to 503).
    """

    def __init__(self, store: WebhookEventStore, secret: str) -> None:
        self.store = store
        self.secret = secret

    async def handle_raw(
        self,
        *,
        body: bytes,
        signature: str | None,
        event_type: str,
        delivery_id: str,
    ) -> WebhookEvent | None:
        """Process one inbound webhook event end-to-end.

        Pipeline (every step is a precondition for the next):
          1. Verify HMAC → :class:`WebhookVerificationError` on fail.
          2. Check ``is_duplicate(delivery_id)`` → return ``None`` if
             this delivery was already seen (redelivery; we don't
             re-process or re-parse).
          3. Parse the JSON body → :class:`WebhookEvent`.
          4. ``record_event(...)`` → returns ``None`` on race
             (another request with the same delivery_id landed
             between our ``is_duplicate`` check and the INSERT).
             We return ``None`` in that case too.

        On any exception during step 3 (e.g. malformed JSON), the
        exception propagates — the route should map it to 400.

        Args:
            body:         Raw request body (NOT pre-parsed).
            signature:    ``X-Hub-Signature-256`` header value (or
                          ``None`` if missing).
            event_type:   ``X-GitHub-Event`` header value.
            delivery_id:  ``X-GitHub-Delivery`` header value (UUID).

        Returns:
            A :class:`WebhookEvent` if the event was new and
            successfully recorded. ``None`` for redeliveries
            (the route should still return 200 in that case).
        """
        # 1. HMAC verify — fails before any expensive work.
        verify_github_signature(
            body=body, signature_header=signature, secret=self.secret,
        )
        # 2. Fast-path duplicate check (avoids the JSON parse).
        if await self.store.is_duplicate(delivery_id):
            logger.info("webhook redelivery: delivery_id=%s", delivery_id)
            return None
        # 3. Parse.
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"webhook body is not valid JSON: {e}") from e
        event = parse_github_payload(event_type, payload)
        # Fill in the delivery_id (the parser uses a placeholder).
        event = event.model_copy(update={"delivery_id": delivery_id})
        # 4. Record (UNIQUE on delivery_id is the canonical
        # idempotency check; ``is_duplicate`` is just a fast-path).
        event_id = await self.store.record_event(
            delivery_id=delivery_id,
            event_type=event_type,
            action=event.action,
            payload=payload,
        )
        if event_id is None:
            # Race: another request with the same delivery_id won
            # the INSERT. Treat as duplicate, skip processing.
            logger.info(
                "webhook race detected: delivery_id=%s (another handler "
                "recorded it first)", delivery_id,
            )
            return None
        return event

    async def dispatch_event(
        self,
        event: WebhookEvent,
        job_store: JobStore,
    ) -> dict[str, Any]:
        """Dispatch a parsed event to the JobStore.

        This is the "what to do with this event" half of the
        pipeline. It runs AFTER :meth:`handle_raw` has verified
        the HMAC and recorded the event in the store.

        Behaviour per event type:
          - ``pull_request`` with ``action="closed"`` and
            ``pr_merged=True`` → look up the job by ``pr_number``
            and mark it ``merged`` (was ``pr_auto_merge_enabled``).
          - ``check_run`` with ``conclusion="failure"`` → look up
            the job and mark it ``failed`` (was ``pr_waiting_checks``).
            ``conclusion="success"`` is currently a no-op (the
            :func:`wait_for_checks` polling loop will pick it up
            on its next iteration; we don't have a
            "short-circuit wait_for_checks" call yet — that's a
            Phase 2.3 follow-up).
          - ``pull_request_review`` with ``review_state="changes_requested"``
            → mark the job ``failed`` (was ``pr_waiting_checks``
            or, in a future Phase 2.4, ``pr_waiting_review``).
          - All other combinations → log + return
            ``{"processed": False, "reason": "no action"}``.

        Returns:
            A small dict describing what happened, suitable for
            serialisation in the route's 200 response. Operators
            can also read it for debugging.
        """
        # Pull a single, latest job matching the pr_number. If no
        # job has this number (the PR was opened by a human, not by
        # the merge queue), we return a no-op.
        if event.pr_number is None:
            return {"processed": False, "reason": "no pr_number in event"}
        job = await job_store.find_job_by_pr_number(event.pr_number)
        if job is None:
            return {
                "processed": False,
                "reason": f"no job with pr_number={event.pr_number}",
            }
        # Terminal statuses: do not re-dispatch. The merge queue
        # has already moved on; this is a no-op for the dispatcher.
        if job.status in ("merged", "failed", "timeout", "cancelled"):
            return {
                "processed": False,
                "reason": f"job {job.id} already in terminal status {job.status}",
            }

        # === pull_request closed+merged ===
        if (
            event.event_type == "pull_request"
            and event.action == "closed"
            and event.pr_merged
        ):
            await job_store.update_status(
                job.id, "merged", finished=True,
                cost=job.cost,
                pr_url=event.pr_url or job.pr_url,
                pr_number=event.pr_number,
            )
            return {
                "processed": True,
                "action": "marked_merged",
                "job_id": job.id,
            }

        # === check_run failure ===
        if (
            event.event_type == "check_run"
            and event.conclusion == "failure"
        ):
            await job_store.update_status(
                job.id, "failed", finished=True,
                cost=job.cost,
                error=f"PR CI failed (check_run reported failure)",
            )
            return {
                "processed": True,
                "action": "marked_failed",
                "job_id": job.id,
                "reason": "check_run failure",
            }

        # === check_run success — short-circuit polling ===
        if (
            event.event_type == "check_run"
            and event.conclusion == "success"
        ):
            # The polling loop in wait_for_checks will pick this up
            # on its next iteration; we don't have a programmatic
            # "force re-check" hook yet. We log + return a no-op
            # so the route still returns 200 (GitHub doesn't retry).
            return {
                "processed": False,
                "reason": "check_run success; polling loop will pick up",
            }

        # === review changes_requested ===
        if (
            event.event_type == "pull_request_review"
            and event.review_state == "changes_requested"
        ):
            await job_store.update_status(
                job.id, "failed", finished=True,
                cost=job.cost,
                error="PR review requested changes",
            )
            return {
                "processed": True,
                "action": "marked_failed",
                "job_id": job.id,
                "reason": "review changes_requested",
            }

        # === review approved — no-op (Phase 2.4 review flow) ===
        if (
            event.event_type == "pull_request_review"
            and event.review_state == "approved"
        ):
            return {
                "processed": False,
                "reason": "review approved; no auto-merge short-circuit in 2.3",
            }

        # Default: log and let it pass.
        return {
            "processed": False,
            "reason": f"unhandled combination: {event.event_type}/{event.action}",
        }


__all__ = [
    "WebhookVerificationError",
    "WebhookEvent",
    "parse_github_payload",
    "verify_github_signature",
    "WebhookHandler",
]
