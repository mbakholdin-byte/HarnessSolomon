"""Outbound webhook dispatcher (Phase 2.5).

Fires HTTP POST events to one or more operator-configured URLs when
critical lifecycle moments happen in the merge queue (e.g. a job
merges, a job fails, a stack of PRs finishes, a PR is waiting for
review). This is the lightweight "notify my dashboard / Slack /
Telegram" layer that sits BETWEEN the in-process ``_emit()`` bus
(Phase 2.1) and the full Phase 4 hook layer with plugin discovery.

Why a separate module (not part of :mod:`harness.agents.merge_queue`):

- **Decoupling.** The merge queue must continue to work even if
  every outbound URL is down. The dispatcher is fire-and-forget
  with bounded retries and timeouts — it cannot block the queue.
- **Testability.** ``OutboundWebhookDispatcher`` accepts an
  optional ``httpx.AsyncClient`` for DI, so unit tests can swap
  in a fake transport without spawning a server. The merge queue
  has no idea outbound is happening.
- **Phase 4 forward compat.** When Phase 4 ships its full hook
  layer (12 hooks + plugin discovery + isolation rules), the
  ``OutboundWebhookDispatcher`` becomes ONE of the registered
  hooks. The merge queue keeps using ``self._outbound.fire(...)``
  and the hook layer takes over.

Design constraints:

- **No new deps.** Uses :mod:`httpx` (already in Phase 0 deps)
  for HTTP. Stdlib :mod:`asyncio` for task scheduling and
  :mod:`logging` for warnings.
- **Fire-and-forget.** :meth:`OutboundWebhookDispatcher.fire`
  returns immediately; the actual HTTP call runs as an
  ``asyncio.create_task`` scheduled on the caller's event loop.
  The merge queue never ``await``s a network response.
- **Bounded retries + timeout.** Each POST gets at most
  ``max_retries + 1`` attempts (initial + retries), with an
  exponential backoff between attempts and a per-attempt HTTP
  timeout. After exhaustion we log a warning and move on; we
  never raise.
- **Pure payload filtering.** The dispatcher fires ONLY for the
  four event kinds documented in the plan (``merged``,
  ``failed``, ``stack_merged``, ``pr_waiting_review``). Other
  events are silently dropped before any network call.

What we DO NOT do here (Phase 4 carryover):

- HMAC signing. We send a plain bearer token instead. Phase 4
  will introduce a signature header + replay protection.
- Webhook target validation. We POST to whatever URL the
  operator configured. The operator is responsible for picking
  trusted endpoints.
- Per-URL routing. The same event goes to every URL. If you
  want different events to different endpoints, configure
  multiple URLs and filter on the receiver.
- Persistent retry queue. A delivery that fails on every
  attempt is dropped. Phase 4 will introduce a durable queue
  (file-backed or SQLite) for at-least-once delivery.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

import httpx


logger = logging.getLogger(__name__)


#: Event kinds we forward to outbound URLs. Other ``_emit`` kinds
#: (``running_code``, ``code_done``, ``running_review``, ``pr_creating``,
#: ``pr_open``, etc.) are deliberately NOT on this list — the goal
#: is "high-signal lifecycle moments", not "every event".
OUTBOUND_EVENT_KINDS: frozenset[str] = frozenset({
    "merged",          # single PR merged (Phase 2.2 path)
    "failed",          # any job failed (single or stack child)
    "stack_merged",    # Phase 2.4 parent orchestrator promoted
    "pr_waiting_review",  # Phase 2.4 long-poll: human review needed
})


def parse_urls(raw: str) -> list[str]:
    """Parse the comma-separated ``outbound_webhook_urls`` setting.

    Splits on commas, trims whitespace, and drops empty strings
    (so a trailing comma in the config doesn't produce an empty
    URL entry that would cause httpx to raise at call time).

    Args:
        raw: The value of ``settings.outbound_webhook_urls``.

    Returns:
        List of trimmed, non-empty URLs in the order they
        appeared. May be empty (the caller treats empty as
        "outbound disabled").
    """
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


class OutboundWebhookDispatcher:
    """Fire-and-forget HTTP POST dispatcher for lifecycle events.

    The merge queue constructs ONE of these at startup (lifespan
    in :mod:`harness.server.app` for the FastAPI server, or the
    CLI dispatcher for one-off runs) and threads it into
    :class:`~harness.agents.merge_queue.MergeQueue` via the
    ``outbound=`` constructor kwarg.

    Args:
        urls:    List of HTTP(S) URLs to POST to. May be empty
                 (in which case :meth:`fire` is a no-op). Parsed
                 via :func:`parse_urls` if you pass the raw
                 settings string.
        token:   Bearer token for the ``Authorization`` header.
                 Empty string = no ``Authorization`` header sent
                 (NOT recommended in production).
        timeout_s:        Per-attempt HTTP timeout (seconds).
        max_retries:      Number of retries on 4xx / 5xx / timeout.
        http_client:      Optional pre-configured
                          :class:`httpx.AsyncClient` (DI for
                          tests). If ``None``, the dispatcher
                          creates its own client and owns its
                          lifecycle.
        backoff_initial_s: Initial backoff between retries.
        backoff_max_s:     Cap on the exponential backoff.
        jitter_s:          Random jitter added to each backoff.
    """

    def __init__(
        self,
        urls: Iterable[str],
        token: str = "",
        *,
        timeout_s: float = 5.0,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 30.0,
        jitter_s: float = 0.25,
    ) -> None:
        # Materialize once (we may iterate the input twice in
        # tests / debug; converting to a tuple is cheap).
        self.urls: tuple[str, ...] = tuple(u for u in urls if u)
        self.token = token
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._owns_client = http_client is None
        self._client: httpx.AsyncClient = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(timeout=timeout_s)
        )
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.jitter_s = jitter_s

    # === Filter ===

    def should_fire(self, event_kind: str) -> bool:
        """Return True if this kind is one of the four we forward.

        Cheap pre-check before the ``create_task`` so we don't
        spawn no-op tasks for ``pr_creating`` / ``running_code``
        / etc. Pure function — safe to call from any thread.
        """
        return event_kind in OUTBOUND_EVENT_KINDS

    # === Public API ===

    def fire(self, event: dict[str, Any]) -> None:
        """Schedule an HTTP POST for one event. Returns immediately.

        The actual delivery runs in an ``asyncio.create_task``
        spawned on the current event loop. We deliberately do
        NOT ``await`` the delivery here — that would couple the
        merge queue to network latency and could stall
        ``_run_job_async`` for many seconds. A delivery that
        fails every retry logs a warning and is silently
        dropped.

        Args:
            event: The event dict (typically a ``JobEvent`` or
                   a small dict with at least ``kind`` and
                   ``job_id`` keys). Sent verbatim as the JSON
                   POST body. NO secrets (token, body text,
                   code) should be in here.
        """
        kind = event.get("kind", "")
        if not self.should_fire(kind):
            return
        if not self.urls:
            return  # outbound disabled (default)
        # Schedule, don't await. The task is dropped on the
        # floor; if the loop shuts down before it runs, the
        # event is lost (acceptable for fire-and-forget).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (called from sync code) — drop.
            logger.warning(
                "outbound: no running event loop; dropping %s for job %s",
                kind, event.get("job_id"),
            )
            return
        loop.create_task(self._deliver(event))

    async def aclose(self) -> None:
        """Close the underlying HTTP client.

        Call this from the FastAPI lifespan's finally block to
        release the connection pool cleanly. If a client was
        injected via DI, we leave it alone (the caller owns it).
        """
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    # === Internals ===

    async def _deliver(self, event: dict[str, Any]) -> None:
        """POST the event to every URL with bounded retries.

        Each URL gets its own retry budget (so one slow target
        doesn't starve the others). We log the final outcome
        (success / exhausted / per-URL failure) but never raise.
        """
        if not self.urls:
            return
        # Per-URL tasks: run them concurrently so a slow
        # receiver doesn't serialize the others.
        await asyncio.gather(
            *(self._deliver_one(url, event) for url in self.urls),
            return_exceptions=True,
        )

    async def _deliver_one(self, url: str, event: dict[str, Any]) -> None:
        """POST to one URL with exponential backoff retries."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Phase 3: redact the event payload before it leaves the
        # process. The current events don't carry raw user text
        # (the merge queue's _emit() payload is metadata only),
        # but defence in depth — if a future event field ever
        # leaks a prompt or PR body, it gets scrubbed here.
        from harness.redaction import redact_dict
        safe_event = redact_dict(event, set(event.keys()))
        last_err: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.post(
                    url, json=safe_event, headers=headers,
                )
            except httpx.TimeoutException as e:
                last_err = f"timeout: {e}"
            except httpx.HTTPError as e:
                # Connection refused, DNS failure, etc. — same
                # retry policy as a 5xx.
                last_err = f"http_error: {e}"
            else:
                if 200 <= resp.status_code < 300:
                    return  # success
                if 400 <= resp.status_code < 500:
                    # 4xx = "client error, won't fix by retrying".
                    # We still log so the operator notices, but
                    # we don't waste retries.
                    logger.warning(
                        "outbound: %s for %s returned %d (no retry): %s",
                        event.get("kind"), url, resp.status_code,
                        resp.text[:200],
                    )
                    return
                # 5xx — retry.
                last_err = f"http {resp.status_code}: {resp.text[:200]}"
            # Backoff between attempts (not after the last one).
            if attempt < self.max_retries:
                backoff = min(
                    self.backoff_initial_s * (2 ** attempt),
                    self.backoff_max_s,
                )
                if self.jitter_s > 0:
                    import random
                    backoff += random.uniform(0, self.jitter_s)
                await asyncio.sleep(backoff)
        logger.warning(
            "outbound: giving up on %s for %s after %d attempt(s): %s",
            event.get("kind"), url, self.max_retries + 1, last_err,
        )


__all__ = [
    "OUTBOUND_EVENT_KINDS",
    "OutboundWebhookDispatcher",
    "parse_urls",
]
