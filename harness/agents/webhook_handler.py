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
    #:
    #: Phase 2.4: this is now the FIRST linked PR (back-compat
    #: for callers that only handle a single PR per event). For
    #: ``check_run`` events, multiple PRs may be linked (a single
    #: check run can validate N PRs against the same SHA). Use
    #: :attr:`pr_numbers` for the full list — the dispatcher
    #: fans out to all of them.
    pr_number: int | None = None
    #: Phase 2.4: list of all PR numbers associated with this
    #: event. For ``check_run`` events this is the full
    #: ``check_run.pull_requests[].number`` list. For
    #: ``pull_request`` / ``pull_request_review`` events this is
    #: ``[event.pr_number]`` (length 1). Used by the dispatcher
    #: to fan out to multiple stacked-PR children.
    pr_numbers: list[int] = Field(default_factory=list)
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
        n = payload.get("number")
        return WebhookEvent(
            delivery_id="",  # filled by handle_raw
            event_type=event_type,
            action=action,
            pr_number=n,
            pr_numbers=[n] if n is not None else [],
            pr_url=pr.get("html_url"),
            head_sha=head.get("sha"),
            pr_merged=bool(pr.get("merged", False)),
        )

    if event_type == "check_run":
        cr = payload.get("check_run") or {}
        # GitHub attaches an array of linked PRs to a check_run. A
        # single check run can validate N PRs against the same SHA
        # (e.g. a CI matrix that re-runs on every dependent PR).
        # Phase 2.4: extract ALL linked PR numbers, not just the
        # first. The dispatcher fans out the event to each PR's
        # job. ``pr_number`` stays as the first for back-compat
        # with Phase 2.3 callers (and single-PR repos).
        linked_prs = cr.get("pull_requests") or []
        pr_numbers_list = [
            int(p["number"])
            for p in linked_prs if p.get("number") is not None
        ]
        first_pr = linked_prs[0] if linked_prs else {}
        return WebhookEvent(
            delivery_id="",
            event_type=event_type,
            action=action,
            pr_number=first_pr.get("number"),
            pr_numbers=pr_numbers_list,
            pr_url=first_pr.get("html_url"),
            head_sha=cr.get("head_sha"),
            conclusion=cr.get("conclusion"),
        )

    if event_type == "pull_request_review":
        review = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        n = pr.get("number") or payload.get("number")
        return WebhookEvent(
            delivery_id="",
            event_type=event_type,
            action=action,
            pr_number=n,
            pr_numbers=[n] if n is not None else [],
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
        merger: Optional Phase 2.4 callable
                ``async def merge_pr(repo, pr_number, env_var) -> None``.
                If set, the dispatcher calls it on a
                ``pull_request_review.approved`` event (instead of
                the Phase 2.3 no-op). If ``None`` (default), the
                approved path stays as a no-op — useful in tests
                that don't want to hit ``gh``.
        auto_merger: Optional Phase 2.4 callable
                ``async def enable_auto_merge(repo, pr_number, ...)``.
                If set, used when ``job.auto_merge=True``. If
                ``None``, the approved path falls back to direct
                ``merger`` (the legacy Phase 2.2 merge flow).
    """

    def __init__(
        self,
        store: WebhookEventStore,
        secret: str,
        *,
        merger: Any = None,
        auto_merger: Any = None,
        outbound: Any = None,
    ) -> None:
        self.store = store
        self.secret = secret
        self._merger = merger
        self._auto_merger = auto_merger
        # Phase 2.5: optional outbound dispatcher. The webhook
        # handler fires ``stack_merged`` events through it after
        # promoting a parent orchestrator row. Default ``None``
        # preserves the Phase 2.4 no-op (no outbound → handler
        # only returns the promotion dict in the response).
        self._outbound = outbound

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
        # Phase 4.1 Step 6.10: emit inbound event at start.
        try:
            from harness.observability import emit_webhook_inbound
            emit_webhook_inbound(event_type=event_type, status="ok", delivery_id=delivery_id)
        except Exception:  # noqa: BLE001
            pass
        # 1. HMAC verify — fails before any expensive work.
        try:
            verify_github_signature(
                body=body, signature_header=signature, secret=self.secret,
            )
        except Exception as exc:
            try:
                from harness.observability import emit_webhook_inbound
                emit_webhook_inbound(
                    event_type=event_type, status="error", delivery_id=delivery_id,
                )
            except Exception:  # noqa: BLE001
                pass
            raise
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
        # Phase 3: redact the inbound payload before persisting. The
        # payload can contain reviewer names, PR titles, comments,
        # and committer emails — all of which may carry PII. The
        # HMAC signature was already verified against the raw body
        # above; redaction happens AFTER signature verification so
        # the canonical hash is preserved.
        from harness.config import settings as _settings
        from harness.redaction import redact_dict as _redact_dict
        if _settings.redaction_enabled:
            payload = _redact_dict(
                payload,
                {"body", "comment.body", "pull_request.body",
                 "issue.body", "review.body", "title", "name",
                 "email", "committer.email", "author.email"},
            )
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

        Phase 2.4: an event may reference multiple PRs
        (``event.pr_numbers``, e.g. a ``check_run`` linked to N
        PRs). We fan out to each PR's job and aggregate the
        results. For ``pull_request`` / ``pull_request_review`` the
        list is always length 1 (back-compat with Phase 2.3).

        After dispatching per-child events, if a child was marked
        ``merged``, we check whether the stack is complete via
        :meth:`JobStore.all_stack_children_merged` and promote the
        parent orchestrator row to ``merged`` if so.

        Behaviour per (event_type, pr_number) pair (Phase 2.3 base,
        extended in Phase 2.4):
          - ``pull_request`` with ``action="closed"`` and
            ``pr_merged=True`` → mark the job ``merged`` (was
            ``pr_auto_merge_enabled``). If the job is part of a
            stack and the stack is now complete, promote parent.
          - ``check_run`` with ``conclusion="failure"`` → mark
            ``failed`` (was ``pr_waiting_checks``).
          - ``check_run`` with ``conclusion="success"`` → no-op
            (the polling loop will pick it up).
          - ``pull_request_review`` with
            ``review_state="changes_requested"`` → mark ``failed``.
          - ``pull_request_review`` with ``review_state="approved"``
            → Phase 2.4 short-circuit: call
            :meth:`_on_review_approved` (merges the PR via the
            injected ``merger`` or ``auto_merger`` callable, or
            no-op if neither is configured).

        Returns:
            A small dict describing what happened, suitable for
            serialisation in the route's 200 response. For fan-out
            events, ``dispatched_to`` lists the job ids we
            processed; ``promoted_parent`` is set if a parent
            orchestrator row was flipped to ``merged``.
        """
        # Phase 2.4: normalise pr_numbers (Phase 2.3 events have
        # only ``pr_number``; check_run can have multiple).
        pr_numbers = list(event.pr_numbers)
        if not pr_numbers and event.pr_number is not None:
            pr_numbers = [event.pr_number]
        if not pr_numbers:
            return {"processed": False, "reason": "no pr_number in event"}

        # Fan out to each PR's job (if any).
        per_job_results: list[dict[str, Any]] = []
        promoted_parent: dict[str, Any] | None = None
        for n in pr_numbers:
            job = await job_store.find_job_by_pr_number(n)
            if job is None:
                per_job_results.append({
                    "pr_number": n,
                    "processed": False,
                    "reason": f"no job with pr_number={n}",
                })
                continue
            r = await self._dispatch_to_job(event, n, job, job_store)
            per_job_results.append({"pr_number": n, **r})
            # Phase 2.4: if this event merged a child AND the
            # child is part of a stack, check if the whole stack
            # is now merged → promote the parent.
            if (
                r.get("action") == "marked_merged"
                and job.pr_stack_id
            ):
                promoted = await self._maybe_promote_stack_parent(
                    job.pr_stack_id, job_store,
                )
                if promoted is not None:
                    promoted_parent = promoted

        # Aggregate the fan-out: if any one was processed, the
        # whole event is "processed". Otherwise it's a no-op.
        any_processed = any(r.get("processed") for r in per_job_results)
        result: dict[str, Any] = {
            "processed": any_processed,
            "dispatched_to": per_job_results,
        }
        if promoted_parent is not None:
            result["promoted_parent"] = promoted_parent
            # Phase 2.5: notify outbound webhook subscribers.
            # The dispatcher filters by ``kind`` (stack_merged is
            # in OUTBOUND_EVENT_KINDS); we hand it the promotion
            # dict and let it decide how to serialise.
            if self._outbound is not None:
                self._outbound.fire(
                    {
                        "event": "stack_merged",
                        "job_id": promoted_parent.get("parent_job_id"),
                        "kind": "stack_merged",
                        "stack_id": promoted_parent.get("stack_id"),
                        "children_count": promoted_parent.get("children_count"),
                    },
                )
        if not any_processed:
            # Preserve the original Phase 2.3 single-result shape
            # for callers that don't expect fan-out.
            if len(per_job_results) == 1:
                result["reason"] = per_job_results[0].get("reason")
            else:
                result["reason"] = (
                    f"no actionable job for {len(pr_numbers)} PR(s)"
                )
        elif len(per_job_results) == 1:
            # Phase 2.3 back-compat: when only one job was
            # affected, also expose ``action`` / ``job_id`` at the
            # top level so existing callers/tests don't have to
            # descend into ``dispatched_to``.
            only = per_job_results[0]
            if "action" in only:
                result["action"] = only["action"]
            if "job_id" in only:
                result["job_id"] = only["job_id"]
        return result

    async def _dispatch_to_job(
        self,
        event: WebhookEvent,
        pr_number: int,
        job: Any,
        job_store: JobStore,
    ) -> dict[str, Any]:
        """Dispatch one event to one job. Inner phase of :meth:`dispatch_event`.

        Phase 2.4 split: was inline in ``dispatch_event``; now
        factored so the fan-out loop can call it per PR.

        Returns a per-job dict (no fan-out aggregation).
        """
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
                pr_number=pr_number,
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
                error="PR CI failed (check_run reported failure)",
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

        # === review approved — Phase 2.4 short-circuit ===
        if (
            event.event_type == "pull_request_review"
            and event.review_state == "approved"
        ):
            return await self._on_review_approved(job, job_store)

        # Default: log and let it pass.
        return {
            "processed": False,
            "reason": f"unhandled combination: {event.event_type}/{event.action}",
        }

    async def _on_review_approved(
        self,
        job: Any,
        job_store: JobStore,
    ) -> dict[str, Any]:
        """Phase 2.4: handle a ``pull_request_review.approved`` event.

        Closes the explicit Phase 2.3 no-op
        (``webhook_handler.py:482-490`` in the pre-Step-3 code).
        Replaces it with a real flow:

          1. If the job is already in a terminal status, no-op.
          2. If ``job.auto_merge=True`` and an ``auto_merger`` is
             injected: call it (``gh pr merge --auto``), set the
             job to ``pr_auto_merge_enabled``, and wait for the
             inbound ``pull_request.closed+merged`` webhook to
             flip it to ``merged``.
          3. Else (no auto-merge): call the injected ``merger``
             directly (``gh pr merge``), set the job to
             ``merged``, finished=True.

        If neither ``merger`` nor ``auto_merger`` is injected
        (e.g. a test environment that doesn't want to hit ``gh``),
        the function returns a no-op and the job stays in its
        current status. This preserves the Phase 2.3 semantics
        for callers that don't wire the new dependencies.

        The merger/auto_merger callables are injected at
        :class:`WebhookHandler` construction (DI) to keep the
        trust boundary: this module does NOT import from
        :mod:`harness.agents.pr_integration` at the top level.
        """
        # 1. Terminal guard.
        if job.status in ("merged", "failed", "timeout", "cancelled"):
            return {
                "processed": False,
                "reason": f"job {job.id} already in terminal status {job.status}",
            }

        # 2. Branch on auto_merge. ``auto_merge`` is a ``MergeJob``
        # field (set at enqueue time); the ``JobRecord`` we read
        # back from the store does NOT carry it (Phase 2.3
        # schema). We default to False for Phase 2.3 / pre-2.4
        # jobs. Phase 2.4 stack children are created with the
        # parent's auto_merge via the stack orchestrator — for
        # now, the operator can wire the ``auto_merger`` callable
        # at server startup to enable this path.
        auto_merge = bool(getattr(job, "auto_merge", False))
        repo = job.repo
        pr_number = job.pr_number
        if auto_merge and self._auto_merger is not None:
            try:
                await self._auto_merger(
                    repo=repo, pr_number=pr_number,
                    merge_method="squash",  # Phase 2.4: respects setting
                    delete_branch=True,
                    env_var="GITHUB_TOKEN",
                )
            except Exception as e:
                return {
                    "processed": False,
                    "reason": f"enable_auto_merge failed: {e}",
                }
            await job_store.update_status(
                job.id, "pr_auto_merge_enabled",
                cost=job.cost,
            )
            return {
                "processed": True,
                "action": "auto_merge_enabled",
                "job_id": job.id,
            }

        # 3. Direct merge path.
        if self._merger is None:
            return {
                "processed": False,
                "reason": (
                    "review approved but no merger injected; "
                    "configure WebhookHandler(merger=...) to enable"
                ),
            }
        try:
            await self._merger(
                repo=repo, pr_number=pr_number,
                env_var="GITHUB_TOKEN",
            )
        except Exception as e:
            await job_store.update_status(
                job.id, "failed", finished=True,
                cost=job.cost,
                error=f"merge_pr after approved review failed: {e}",
            )
            return {
                "processed": True,
                "action": "marked_failed",
                "job_id": job.id,
                "reason": f"merge failed after approved review: {e}",
            }
        await job_store.update_status(
            job.id, "merged", finished=True,
            cost=job.cost,
        )
        return {
            "processed": True,
            "action": "merged_via_review",
            "job_id": job.id,
        }

    async def _maybe_promote_stack_parent(
        self,
        stack_id: str,
        job_store: JobStore,
    ) -> dict[str, Any] | None:
        """If all children of a stack are merged, promote the parent.

        Phase 2.4: after a child PR is marked ``merged`` (via the
        ``pull_request.closed+merged`` webhook), the parent
        orchestrator row stays in ``pr_open`` until ALL children
        are merged. This helper checks the aggregate via
        :meth:`JobStore.all_stack_children_merged` and, if True,
        updates the parent row to ``merged``.

        Returns the promotion dict (for the route's 200 response)
        or ``None`` if no promotion was needed.
        """
        all_merged = await job_store.all_stack_children_merged(stack_id)
        if not all_merged:
            return None
        rows = await job_store.find_jobs_by_stack_id(stack_id)
        parent = next(
            (r for r in rows if r.stack_position == 0),
            None,
        )
        if parent is None:
            return None
        if parent.status in ("merged", "failed", "timeout", "cancelled"):
            return None  # already terminal
        # Sum cost across children + parent (best effort; the
        # children may not have a meaningful cost if they were
        # created as PR rows directly).
        total_cost = sum(r.cost for r in rows)
        await job_store.update_status(
            parent.id, "merged", finished=True,
            cost=total_cost,
        )
        return {
            "stack_id": stack_id,
            "parent_job_id": parent.id,
            "children_count": len(rows) - 1,
        }


__all__ = [
    "WebhookVerificationError",
    "WebhookEvent",
    "parse_github_payload",
    "verify_github_signature",
    "WebhookHandler",
]
