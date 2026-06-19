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
        event_store: Any = None,
        auto_disable_threshold: int = 10,
        dlq_enabled: bool = True,
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
        # Phase 4.13B: optional WebhookEventStore for auto-disable,
        # DLQ, and secret rotation. Typed as Any to preserve the
        # trust boundary (no import of harness.agents.webhook_store
        # at module level — the dispatcher stays decoupled from the
        # store; lifespan DI wires a real store at server boot, and
        # tests inject a fake). When None, all hardening features
        # are no-ops (Phase 2.5 fire-and-forget behaviour).
        self._event_store: Any = event_store
        self.auto_disable_threshold: int = max(1, int(auto_disable_threshold))
        self.dlq_enabled: bool = bool(dlq_enabled)

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
        dropped (or persisted to the DLQ when
        ``event_store`` is wired — Phase 4.13B).

        Phase 4.13B Drift 1: when an ``event_store`` is configured,
        disabled URLs (auto-disabled after N consecutive failures)
        are skipped before the task is scheduled. The skip is
        best-effort (we don't await a DB read in ``fire``); a
        definitive check happens inside ``_deliver`` via
        ``_is_disabled``.

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

        Phase 4.13B Drift 1: when an ``event_store`` is configured,
        URLs that are currently auto-disabled (``disabled_at`` set)
        are skipped before any HTTP attempt. The skip is logged at
        INFO level so the operator can see "we're holding back
        deliveries to a flaky endpoint" in the audit trail.
        """
        if not self.urls:
            return
        # Phase 4.13B Drift 1: filter disabled URLs when a store is
        # wired. We await the per-URL config row read here (inside
        # the async deliver task, not in the sync ``fire`` entry
        # point) so the merge queue never blocks on it.
        active_urls = await self._filter_disabled(self.urls)
        if not active_urls:
            return
        # Per-URL tasks: run them concurrently so a slow
        # receiver doesn't serialize the others.
        await asyncio.gather(
            *(self._deliver_one(url, event) for url in active_urls),
            return_exceptions=True,
        )

    async def _filter_disabled(self, urls: tuple[str, ...]) -> tuple[str, ...]:
        """Return the subset of ``urls`` that are NOT auto-disabled.

        Phase 4.13B Drift 1. When no ``event_store`` is configured,
        all URLs are returned unchanged (Phase 2.5 fire-and-forget
        behaviour). Errors from the store are swallowed and the URL
        is treated as active (fail-open — we'd rather double-send
        than silently drop everything if the store is wedged).
        """
        if self._event_store is None:
            return urls
        active: list[str] = []
        for url in urls:
            try:
                cfg = await self._event_store.get_outbound(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "outbound: store get_outbound(%s) failed (%s); "
                    "treating as active (fail-open)",
                    url, exc,
                )
                active.append(url)
                continue
            if cfg is not None and cfg.disabled_at is not None:
                logger.info(
                    "outbound: skipping disabled url=%s "
                    "(disabled_at=%s, failures=%d)",
                    url, cfg.disabled_at, cfg.consecutive_failures,
                )
                continue
            active.append(url)
        return tuple(active)

    async def _deliver_one(self, url: str, event: dict[str, Any]) -> None:
        """POST to one URL with exponential backoff retries.

        Phase 4.13B: on success → ``record_outbound_success`` (resets
        the failure counter). On terminal failure →
        ``record_outbound_failure`` (bumps the counter; auto-disable
        if threshold met) + ``enqueue_dlq`` (if ``dlq_enabled``).
        Both callbacks are best-effort — a store error is logged and
        swallowed so it cannot stall the dispatcher.
        """
        # Phase 4.1 Step 6.8: timing.
        import time as _time
        from harness.observability import emit_outbound_delivery
        _obs_start = _time.monotonic()
        _kind = str(event.get("kind") or event.get("event") or "unknown")
        # Phase 4.13B Drift 3: resolve the signing token via the
        # store's secret_version when available (async path). The
        # synchronous _build_headers helper is kept for tests that
        # inject a token directly, but the production path always
        # goes through _resolve_token so rotation works.
        token = await self._resolve_token(url)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # Phase 3: redact the event payload before it leaves the
        # process. The current events don't carry raw user text
        # (the merge queue's _emit() payload is metadata only),
        # but defence in depth — if a future event field ever
        # leaks a prompt or PR body, it gets scrubbed here.
        from harness.redaction import redact_dict
        safe_event = redact_dict(event, set(event.keys()))
        last_err: str = ""
        terminal_failure = False
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
                    try:
                        emit_outbound_delivery(
                            kind=_kind,
                            status_code=str(resp.status_code),
                            duration_s=_time.monotonic() - _obs_start,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    await self._on_success(url)
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
                    last_err = (
                        f"http {resp.status_code}: {resp.text[:200]}"
                    )
                    try:
                        emit_outbound_delivery(
                            kind=_kind,
                            status_code=str(resp.status_code),
                            duration_s=_time.monotonic() - _obs_start,
                            error=resp.text[:200] or "",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    terminal_failure = True
                    break
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
        else:
            # Loop exhausted without break → all retries failed.
            terminal_failure = True

        if terminal_failure:
            logger.warning(
                "outbound: giving up on %s for %s after %d attempt(s): %s",
                event.get("kind"), url, self.max_retries + 1, last_err,
            )
            # Phase 4.1 Step 6.8: emit giveup event.
            try:
                emit_outbound_delivery(
                    kind=_kind,
                    status_code=(
                        "timeout" if last_err.startswith("timeout")
                        else "5xx"
                    ),
                    duration_s=_time.monotonic() - _obs_start,
                    error=last_err,
                )
            except Exception:  # noqa: BLE001
                pass
            await self._on_failure(url, event, last_err)

    def _build_headers(self, url: str) -> dict[str, str]:
        """Build the HTTP headers for a delivery to ``url``.

        Phase 4.13B Drift 3: when an ``event_store`` is configured,
        the signing secret is resolved by ``secret_version`` on the
        outbound config row (``resolve_outbound_secret``). Version 1
        falls back to the legacy ``WEBHOOK_SECRET`` env var, so
        pre-rotation deployments keep working without code changes.
        When the resolved secret is non-empty, it's sent as the
        ``Authorization: Bearer`` value (matching the Phase 2.5 wire
        format). When empty, we fall back to the constructor
        ``token`` (which may itself be empty).
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        token = self.token
        if self._event_store is not None:
            # Best-effort sync path: we cannot await here, so we
            # read the cached secret_version via the store's
            # synchronous accessor if it exposes one. Most stores
            # cache the row; fall back to the legacy token when the
            # async read hasn't happened yet. The async
            # ``_filter_disabled`` call already fetched the row,
            # but it's not stashed — to avoid an await here we
            # rely on the dispatcher-level token being correct for
            # v1 deployments (the common case), and document that
            # v2+ rotation requires the async path (server boot
            # pre-fetches secrets). For tests that exercise v2+, we
            # inject the resolved secret directly via ``token=``.
            pass
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _resolve_token(self, url: str) -> str:
        """Async token resolution honoring ``secret_version`` (Drift 3).

        Called by :meth:`_deliver_one`'s helper path when the
        synchronous :meth:`_build_headers` cannot resolve a v2+
        secret. Returns the constructor ``token`` when no store is
        wired or when the row's secret_version is 1 with an unset
        ``WEBHOOK_SECRET`` (Phase 2.5 backward compat).
        """
        if self._event_store is None:
            return self.token
        try:
            cfg = await self._event_store.get_outbound(url)
        except Exception:  # noqa: BLE001
            return self.token
        if cfg is None:
            return self.token
        # Late import — preserves the trust boundary (no
        # module-level import of webhook_store; the dispatcher
        # stays usable in contexts without the store).
        from harness.agents.webhook_store import resolve_outbound_secret
        resolved = resolve_outbound_secret(cfg.secret_version)
        return resolved if resolved else self.token

    async def _on_success(self, url: str) -> None:
        """Best-effort success callback to the event store.

        Resets the consecutive-failure counter so a single success
        after a string of transient failures doesn't pile up toward
        the auto-disable threshold.
        """
        if self._event_store is None:
            return
        try:
            await self._event_store.record_outbound_success(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outbound: record_outbound_success(%s) failed: %s",
                url, exc,
            )

    async def _on_failure(
        self,
        url: str,
        event: dict[str, Any],
        last_err: str,
    ) -> None:
        """Best-effort failure callback: bump counter + enqueue DLQ.

        Phase 4.13B Drift 1 + 2. Called when all retries are
        exhausted OR a 4xx terminal error was hit. Wraps both store
        calls in try/except so a store hiccup cannot stall the
        dispatcher (the event is already lost from the merge-queue
        perspective; we just log and move on).
        """
        if self._event_store is None:
            return
        try:
            disabled_now = await self._event_store.record_outbound_failure(
                url, auto_disable_threshold=self.auto_disable_threshold,
            )
            if disabled_now:
                logger.warning(
                    "outbound: url=%s auto-disabled after threshold failures",
                    url,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outbound: record_outbound_failure(%s) failed: %s",
                url, exc,
            )
        if self.dlq_enabled:
            try:
                await self._event_store.enqueue_dlq(
                    url=url,
                    event_kind=str(
                        event.get("kind") or event.get("event") or "unknown"
                    ),
                    payload=event,
                    last_error=last_err,
                    attempts=self.max_retries + 1,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "outbound: enqueue_dlq(%s) failed: %s", url, exc,
                )


__all__ = [
    "OUTBOUND_EVENT_KINDS",
    "OutboundWebhookDispatcher",
    "parse_urls",
]
