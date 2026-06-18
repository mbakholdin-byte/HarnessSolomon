"""Phase 4.1: HealthChecker — liveness / readiness / deep probes.
Phase 4.9 Task C: 8 subsystem-level deep probes.

Three health endpoints, mirror Kubernetes liveness/readiness pattern:
    - ``/health/live`` — process up, returns 200 always.
    - ``/health/ready`` — dependencies (Qdrant/SQLite/Neo4j) reachable.
    - ``/health/deep`` — full diagnostics (queue depth, hook registry, etc.).

Phase 4.9 Task C adds 8 subsystem probes:
    - db              (critical):    SELECT 1 on agent-jobs.db
    - qdrant          (non-critical): GET /_health
    - opensearch      (non-critical): GET /_cluster/health
    - job_store       (critical):     JobStore.count_jobs()
    - merge_queue     (non-critical): MergeQueue.stats()
    - elicitation_broker (non-critical): broker.stats()
    - notify_channels (non-critical): per-channel config check
    - rate_limiter    (non-critical): rate_limiter.check()

Trust boundary: stdlib + asyncio only. Probes are injected via
``HealthChecker.__init__`` kwargs (DI), NOT imported from
``harness.agents`` / ``harness.server`` (AST-enforced by
``tests/test_observability_trust_boundary.py``).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

HealthStatus = Literal["ok", "degraded", "unhealthy"]
# Phase 4.9 Task C: deep-probe statuses use the same vocabulary as the
# aggregate status. Each ProbeResult reports one of these.
ProbeStatus = Literal["ok", "degraded", "down", "skipped", "timeout", "error"]


@dataclass(frozen=True)
class ProbeResult:
    """Per-probe result returned by deep() subsystem probes.

    Attributes:
        status: ``ok`` / ``degraded`` / ``down`` / ``skipped`` / ``timeout`` / ``error``.
            ``skipped`` = optional dependency not configured.
            ``timeout`` / ``error`` = probe failed but did not crash deep().
        latency_ms: Wall-clock latency in milliseconds (rounded to 3 decimals).
        message: Human-readable status string (e.g. "SELECT 1 ok",
            "HTTP 503", "Qdrant URL not configured").
    """

    status: ProbeStatus
    latency_ms: float
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "latency_ms": round(self.latency_ms, 3),
            "message": self.message,
        }


@dataclass(frozen=True)
class HealthReport:
    """Result of a health check.

    Attributes:
        status: Overall status (``ok`` / ``degraded`` / ``unhealthy``).
        checks: Per-probe results, keyed by probe name. Backward-compat
            with Phase 4.1: this is the legacy registry-style checks
            dict (used by liveness/readiness/legacy deep()).
        version: Harness version (set by caller).
        project_root: Project root path (set by caller).
        probes: Phase 4.9 Task C subsystem probe results. Empty for
            liveness / readiness. Populated only by deep() when
            subsystem kwargs are configured. Each value is a
            :class:`ProbeResult`.
        ts: ISO-8601 timestamp (UTC, with 'Z' suffix). Set when the
            report is built. Empty for legacy callers.
    """

    status: HealthStatus
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: str = ""
    project_root: str = ""
    probes: dict[str, ProbeResult] = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "version": self.version,
            "project_root": self.project_root,
            "checks": self.checks,
            "probes": {k: v.to_dict() for k, v in self.probes.items()},
            "ts": self.ts,
        }
        return out


# Probe = async callable that returns (status_dict, ok_bool).
# Used by the legacy register_probe() API (Phase 4.1 liveness/readiness/old deep).
Probe = Callable[[], Awaitable[tuple[dict[str, Any], bool]]]


# === Phase 4.9 Task C: probe timeouts and criticality ===
_DEFAULT_PROBE_TIMEOUT_S: float = 2.0


class HealthChecker:
    """Aggregates health probes for /health/{live,ready,deep}.

    Probes are DI'd via ``register_probe(name, probe)`` (Phase 4.1
    legacy API) and via ``__init__`` kwargs (Phase 4.9 Task C
    subsystem probes). This module does NOT import Qdrant / Neo4j /
    SQLite directly — that's the caller's job (B1 mirror).

    Phase 4.9 Task C deep() behaviour:
        - If no subsystem kwargs configured → falls back to the legacy
          registry-based deep() (backward-compat with Phase 4.1 tests).
        - If ANY subsystem kwarg configured → runs the 8 subsystem
          probes in parallel and returns a HealthReport with populated
          ``probes`` field. The legacy ``checks`` dict mirrors probe
          statuses for ops dashboards that still consume ``checks``.
    """

    # Critical probe names: failure of any one → status "down".
    _CRITICAL_PROBES: frozenset[str] = frozenset({"db", "job_store"})

    def __init__(
        self,
        version: str = "1.7.0",
        project_root: str = "",
        *,
        # Phase 4.9 Task C: 9 new optional kwargs (8 probes + 1 unused
        # circuit_breaker slot for future use; not currently probed).
        db_path: Any | None = None,
        qdrant_url: str | None = None,
        opensearch_url: str | None = None,
        job_store: Any = None,
        merge_queue: Any = None,
        elicitation_broker: Any = None,
        notify_channels: list[str] | None = None,
        rate_limiter: Any = None,
        circuit_breaker: Any = None,
    ) -> None:
        self._version = version
        self._project_root = project_root
        self._probes: dict[str, Probe] = {}
        self._ready_timeout_s: float = 2.0
        self._deep_timeout_s: float = 5.0
        self._require_qdrant: bool = False
        self._require_neo4j: bool = False

        # Phase 4.9 Task C subsystem DI handles. We keep ``Any`` to
        # avoid importing the production classes (trust boundary).
        self._db_path = db_path
        self._qdrant_url = qdrant_url
        self._opensearch_url = opensearch_url
        self._job_store = job_store
        self._merge_queue = merge_queue
        self._elicitation_broker = elicitation_broker
        self._notify_channels = (
            list(notify_channels) if notify_channels is not None else None
        )
        self._rate_limiter = rate_limiter
        self._circuit_breaker = circuit_breaker  # reserved

    # === Configuration ===
    def configure(
        self,
        *,
        ready_timeout_s: float = 2.0,
        deep_timeout_s: float = 5.0,
        require_qdrant: bool = False,
        require_neo4j: bool = False,
    ) -> None:
        """Set probe timeouts and required-probe policy."""
        self._ready_timeout_s = ready_timeout_s
        self._deep_timeout_s = deep_timeout_s
        self._require_qdrant = require_qdrant
        self._require_neo4j = require_neo4j

    def register_probe(self, name: str, probe: Probe) -> None:
        """Register a probe by name. Replaces if already registered."""
        self._probes[name] = probe

    def unregister_probe(self, name: str) -> bool:
        return self._probes.pop(name, None) is not None

    # === Endpoints ===
    async def liveness(self) -> HealthReport:
        """Liveness = process is up. Always 200 if we get here."""
        return HealthReport(
            status="ok",
            version=self._version,
            project_root=self._project_root,
            checks={"process": {"status": "ok"}},
        )

    async def readiness(self) -> HealthReport:
        """Readiness = critical dependencies reachable.

        Returns ``unhealthy`` if any probe in ``_require_*`` fails.
        Returns ``degraded`` if a non-required probe fails.
        Returns ``ok`` if all pass.
        """
        results = await self._run_all_probes(self._ready_timeout_s)
        return self._aggregate(results, require_mode=True)

    async def deep(self) -> HealthReport:
        """Deep = full diagnostics, all probes, longer timeout.

        Phase 4.9 Task C: if subsystem kwargs are configured (db_path,
        qdrant_url, etc.), this method runs the 8 subsystem probes in
        parallel via ``asyncio.gather(return_exceptions=True)`` and
        returns a HealthReport with a populated ``probes`` dict.

        Each probe is wrapped in ``asyncio.wait_for(timeout=2.0)``.
        Critical probes (``db``, ``job_store``): failure → status "down".
        Non-critical probes: failure → status "degraded".
        All probes passing → status "ok".

        If NO subsystem kwargs are configured, falls back to the
        Phase 4.1 legacy registry-based deep() (backward-compat).
        """
        if not self._has_subsystem_config():
            # Phase 4.1 legacy path: aggregate registered probes.
            results = await self._run_all_probes(self._deep_timeout_s)
            return self._aggregate(results, require_mode=False)

        # Phase 4.9 Task C: run all 8 subsystem probes in parallel.
        probe_coros = [
            self._probe_db(),
            self._probe_qdrant(),
            self._probe_opensearch(),
            self._probe_job_store(),
            self._probe_merge_queue(),
            self._probe_elicitation_broker(),
            self._probe_notify_channels(),
            self._probe_rate_limiter(),
        ]
        # return_exceptions=True so one probe failure doesn't cancel the rest.
        raw_results = await asyncio.gather(*probe_coros, return_exceptions=True)

        probe_results: dict[str, ProbeResult] = {}
        for i, raw in enumerate(raw_results):
            name = self._probe_names()[i]
            if isinstance(raw, ProbeResult):
                probe_results[name] = raw
            elif isinstance(raw, BaseException):
                # Shouldn't happen — each _probe_* wraps in try/except
                # and returns a ProbeResult. But if a probe raises
                # (e.g. due to a bug), we surface it as status "error".
                probe_results[name] = ProbeResult(
                    status="error",
                    latency_ms=0.0,
                    message=f"probe raised: {type(raw).__name__}: {raw}",
                )
            else:
                probe_results[name] = ProbeResult(
                    status="error",
                    latency_ms=0.0,
                    message=f"unexpected probe result type: {type(raw).__name__}",
                )

        # Aggregate into overall status.
        status = self._aggregate_probe_status(probe_results)

        # Mirror probe statuses into legacy ``checks`` dict so ops
        # dashboards that consume ``checks`` still see probe data.
        checks_mirror: dict[str, dict[str, Any]] = {
            name: result.to_dict() for name, result in probe_results.items()
        }

        return HealthReport(
            status=status,
            checks=checks_mirror,
            probes=probe_results,
            version=self._version,
            project_root=self._project_root,
            ts=self._utc_now_iso(),
        )

    # === Phase 4.9 Task C: probe methods ===

    async def _probe_db(self) -> ProbeResult:
        """Critical probe 1/8: SELECT 1 on agent-jobs.db.

        Uses the stdlib ``sqlite3`` module directly (no async). The
        SQLite call is fast (<1ms local file) so blocking the event
        loop briefly is acceptable. We wrap in
        ``asyncio.wait_for(timeout=2.0)`` to bound the worst case.
        """
        if self._db_path is None:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="db_path not configured",
            )

        async def _run() -> ProbeResult:
            start = time.monotonic()
            try:
                # sqlite3.connect + execute + close. Path may be a
                # pathlib.Path or str; str() handles both.
                conn = sqlite3.connect(
                    str(self._db_path),
                    timeout=1.0,  # SQLite-level busy timeout
                )
                try:
                    cur = conn.execute("SELECT 1")
                    row = cur.fetchone()
                    ok = row is not None and row[0] == 1
                    latency = (time.monotonic() - start) * 1000.0
                    if ok:
                        return ProbeResult(
                            status="ok",
                            latency_ms=latency,
                            message="SELECT 1 ok",
                        )
                    return ProbeResult(
                        status="down",
                        latency_ms=latency,
                        message=f"SELECT 1 returned unexpected row: {row!r}",
                    )
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"sqlite3 error: {type(exc).__name__}: {exc}",
                )
            except OSError as exc:
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"OS error: {type(exc).__name__}: {exc}",
                )

        try:
            return await asyncio.wait_for(_run(), timeout=_DEFAULT_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ProbeResult(
                status="timeout",
                latency_ms=_DEFAULT_PROBE_TIMEOUT_S * 1000.0,
                message=f"db probe timed out after {_DEFAULT_PROBE_TIMEOUT_S}s",
            )

    async def _probe_qdrant(self) -> ProbeResult:
        """Non-critical probe 2/8: HTTP GET /_health on Qdrant URL.

        Uses stdlib ``urllib.request`` (no aiohttp/httpx dep). The
        call is blocking but bounded by the 2s timeout. Qdrant's
        ``/_health`` returns 200 + ``{"status": "ok"}`` on a healthy
        node; anything else is degraded.
        """
        if not self._qdrant_url:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="qdrant_url not configured",
            )

        async def _run() -> ProbeResult:
            start = time.monotonic()
            url = self._qdrant_url.rstrip("/") + "/_health"
            try:
                req = Request(url, method="GET")
                # socket-level timeout is the lower bound; we also
                # have asyncio.wait_for as the upper bound.
                with urlopen(req, timeout=_DEFAULT_PROBE_TIMEOUT_S) as resp:
                    status_code = getattr(resp, "status", None) or resp.getcode()
                    latency = (time.monotonic() - start) * 1000.0
                    if 200 <= status_code < 300:
                        return ProbeResult(
                            status="ok",
                            latency_ms=latency,
                            message=f"HTTP {status_code}",
                        )
                    return ProbeResult(
                        status="down",
                        latency_ms=latency,
                        message=f"HTTP {status_code}",
                    )
            except URLError as exc:
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"URLError: {exc.reason}",
                )
            except OSError as exc:
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"OS error: {type(exc).__name__}: {exc}",
                )

        try:
            return await asyncio.wait_for(_run(), timeout=_DEFAULT_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ProbeResult(
                status="timeout",
                latency_ms=_DEFAULT_PROBE_TIMEOUT_S * 1000.0,
                message=f"qdrant probe timed out after {_DEFAULT_PROBE_TIMEOUT_S}s",
            )

    async def _probe_opensearch(self) -> ProbeResult:
        """Non-critical probe 3/8: HTTP GET /_cluster/health on OpenSearch.

        Mirrors the Qdrant probe pattern. OpenSearch returns 200 +
        ``{"status": "green"/"yellow"/"red"}``. We accept 2xx as ok;
        anything else is degraded.
        """
        if not self._opensearch_url:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="opensearch_url not configured",
            )

        async def _run() -> ProbeResult:
            start = time.monotonic()
            url = self._opensearch_url.rstrip("/") + "/_cluster/health"
            try:
                req = Request(url, method="GET")
                with urlopen(req, timeout=_DEFAULT_PROBE_TIMEOUT_S) as resp:
                    status_code = getattr(resp, "status", None) or resp.getcode()
                    latency = (time.monotonic() - start) * 1000.0
                    if 200 <= status_code < 300:
                        return ProbeResult(
                            status="ok",
                            latency_ms=latency,
                            message=f"HTTP {status_code}",
                        )
                    return ProbeResult(
                        status="down",
                        latency_ms=latency,
                        message=f"HTTP {status_code}",
                    )
            except URLError as exc:
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"URLError: {exc.reason}",
                )
            except OSError as exc:
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"OS error: {type(exc).__name__}: {exc}",
                )

        try:
            return await asyncio.wait_for(_run(), timeout=_DEFAULT_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ProbeResult(
                status="timeout",
                latency_ms=_DEFAULT_PROBE_TIMEOUT_S * 1000.0,
                message=f"opensearch probe timed out after {_DEFAULT_PROBE_TIMEOUT_S}s",
            )

    async def _probe_job_store(self) -> ProbeResult:
        """Critical probe 4/8: JobStore.count_jobs() returns an int.

        Duck-typed: ``self._job_store`` must expose an async
        ``count_jobs()`` method returning a non-negative int. We
        don't import the real JobStore class (trust boundary).
        """
        if self._job_store is None:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="job_store not configured",
            )

        async def _run() -> ProbeResult:
            start = time.monotonic()
            try:
                count = await self._job_store.count_jobs()
                latency = (time.monotonic() - start) * 1000.0
                if isinstance(count, int) and count >= 0:
                    return ProbeResult(
                        status="ok",
                        latency_ms=latency,
                        message=f"count_jobs={count}",
                    )
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"count_jobs returned non-int/negative: {count!r}",
                )
            except Exception as exc:  # noqa: BLE001 — surface any failure
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"job_store error: {type(exc).__name__}: {exc}",
                )

        try:
            return await asyncio.wait_for(_run(), timeout=_DEFAULT_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ProbeResult(
                status="timeout",
                latency_ms=_DEFAULT_PROBE_TIMEOUT_S * 1000.0,
                message=f"job_store probe timed out after {_DEFAULT_PROBE_TIMEOUT_S}s",
            )

    async def _probe_merge_queue(self) -> ProbeResult:
        """Non-critical probe 5/8: MergeQueue.stats() returns a dict.

        Duck-typed: ``self._merge_queue`` must expose a ``stats()``
        method. May be sync or async (we await if it returns a
        coroutine; otherwise treat the sync result directly).
        """
        if self._merge_queue is None:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="merge_queue not configured",
            )

        async def _run() -> ProbeResult:
            start = time.monotonic()
            try:
                stats = self._merge_queue.stats()
                # If stats() returned a coroutine, await it.
                if asyncio.iscoroutine(stats):
                    stats = await stats
                latency = (time.monotonic() - start) * 1000.0
                if isinstance(stats, dict):
                    return ProbeResult(
                        status="ok",
                        latency_ms=latency,
                        message=f"stats keys: {sorted(stats.keys())}",
                    )
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"stats() returned non-dict: {type(stats).__name__}",
                )
            except Exception as exc:  # noqa: BLE001 — non-critical probe
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"merge_queue error: {type(exc).__name__}: {exc}",
                )

        try:
            return await asyncio.wait_for(_run(), timeout=_DEFAULT_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ProbeResult(
                status="timeout",
                latency_ms=_DEFAULT_PROBE_TIMEOUT_S * 1000.0,
                message=f"merge_queue probe timed out after {_DEFAULT_PROBE_TIMEOUT_S}s",
            )

    async def _probe_elicitation_broker(self) -> ProbeResult:
        """Non-critical probe 6/8: ElicitationBroker.stats() returns a dict.

        Mirrors the merge_queue probe pattern.
        """
        if self._elicitation_broker is None:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="elicitation_broker not configured",
            )

        async def _run() -> ProbeResult:
            start = time.monotonic()
            try:
                stats = self._elicitation_broker.stats()
                if asyncio.iscoroutine(stats):
                    stats = await stats
                latency = (time.monotonic() - start) * 1000.0
                if isinstance(stats, dict):
                    return ProbeResult(
                        status="ok",
                        latency_ms=latency,
                        message=f"stats keys: {sorted(stats.keys())}",
                    )
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"stats() returned non-dict: {type(stats).__name__}",
                )
            except Exception as exc:  # noqa: BLE001 — non-critical probe
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"elicitation_broker error: {type(exc).__name__}: {exc}",
                )

        try:
            return await asyncio.wait_for(_run(), timeout=_DEFAULT_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ProbeResult(
                status="timeout",
                latency_ms=_DEFAULT_PROBE_TIMEOUT_S * 1000.0,
                message=f"elicitation_broker probe timed out after {_DEFAULT_PROBE_TIMEOUT_S}s",
            )

    async def _probe_notify_channels(self) -> ProbeResult:
        """Non-critical probe 7/8: per-channel config check.

        Per the handoff (line 282): we do NOT send real Slack/Teams
        POSTs. We just verify the channels list is well-formed (each
        entry is a non-empty string). An empty list is treated as
        "ok" — no channels configured = no channels to check.
        """
        if self._notify_channels is None:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="notify_channels not configured",
            )

        start = time.monotonic()
        # Synchronous config check — no I/O, no need for wait_for.
        bad: list[str] = []
        for ch in self._notify_channels:
            if not isinstance(ch, str) or not ch.strip():
                bad.append(repr(ch))
        latency = (time.monotonic() - start) * 1000.0
        if bad:
            return ProbeResult(
                status="down",
                latency_ms=latency,
                message=f"invalid channel entries: {bad}",
            )
        return ProbeResult(
            status="ok",
            latency_ms=latency,
            message=f"{len(self._notify_channels)} channel(s) configured",
        )

    async def _probe_rate_limiter(self) -> ProbeResult:
        """Non-critical probe 8/8: HookRateLimiter.check() returns True.

        Duck-typed: ``self._rate_limiter`` must expose a ``check()``
        method. May be sync or async. We expect True (rate limit OK)
        on a healthy limiter; False or raise → degraded.
        """
        if self._rate_limiter is None:
            return ProbeResult(
                status="skipped",
                latency_ms=0.0,
                message="rate_limiter not configured",
            )

        async def _run() -> ProbeResult:
            start = time.monotonic()
            try:
                result = self._rate_limiter.check()
                if asyncio.iscoroutine(result):
                    result = await result
                latency = (time.monotonic() - start) * 1000.0
                if result is True or result is None:
                    # None is acceptable: some limiters return None
                    # to mean "not rate-limited" (no decision made).
                    return ProbeResult(
                        status="ok",
                        latency_ms=latency,
                        message="check() ok",
                    )
                if result is False:
                    return ProbeResult(
                        status="down",
                        latency_ms=latency,
                        message="check() returned False (rate-limited)",
                    )
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"check() returned unexpected: {result!r}",
                )
            except Exception as exc:  # noqa: BLE001 — non-critical probe
                latency = (time.monotonic() - start) * 1000.0
                return ProbeResult(
                    status="down",
                    latency_ms=latency,
                    message=f"rate_limiter error: {type(exc).__name__}: {exc}",
                )

        try:
            return await asyncio.wait_for(_run(), timeout=_DEFAULT_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return ProbeResult(
                status="timeout",
                latency_ms=_DEFAULT_PROBE_TIMEOUT_S * 1000.0,
                message=f"rate_limiter probe timed out after {_DEFAULT_PROBE_TIMEOUT_S}s",
            )

    # === Phase 4.9 Task C: helpers ===

    def _has_subsystem_config(self) -> bool:
        """True if ANY of the 8 subsystem kwargs is non-None/non-empty."""
        return any(
            v is not None
            for v in (
                self._db_path,
                self._qdrant_url,
                self._opensearch_url,
                self._job_store,
                self._merge_queue,
                self._elicitation_broker,
                self._notify_channels,
                self._rate_limiter,
            )
        )

    def _probe_names(self) -> list[str]:
        """Ordered probe names matching the gather() call order in deep()."""
        return [
            "db",
            "qdrant",
            "opensearch",
            "job_store",
            "merge_queue",
            "elicitation_broker",
            "notify_channels",
            "rate_limiter",
        ]

    def _aggregate_probe_status(
        self, probes: dict[str, ProbeResult]
    ) -> HealthStatus:
        """Aggregate per-probe statuses into overall HealthStatus.

        Rules:
            - Any CRITICAL probe (``db``, ``job_store``) with status
              in {``down``, ``timeout``, ``error``} → ``unhealthy``.
            - Else if any non-critical probe has status in
              {``down``, ``timeout``, ``error``} → ``degraded``.
            - Else (all probes ok/skipped) → ``ok``.

        ``skipped`` is treated as "not configured" — it does NOT
        affect the aggregate status. This means a checker with only
        optional deps configured can still report ``ok``.
        """
        bad_critical = False
        bad_non_critical = False
        for name, result in probes.items():
            if result.status in ("ok", "skipped", "degraded"):
                # ``degraded`` here is a per-probe signal that the
                # underlying service is partially available. We don't
                # treat it as a hard failure — the aggregate decides.
                # For now, treat per-probe "degraded" the same as "ok"
                # unless the probe explicitly reports "down"/"timeout"/"error".
                continue
            # status in {down, timeout, error}
            if name in self._CRITICAL_PROBES:
                bad_critical = True
            else:
                bad_non_critical = True
        if bad_critical:
            return "unhealthy"
        if bad_non_critical:
            return "degraded"
        return "ok"

    @staticmethod
    def _utc_now_iso() -> str:
        """ISO-8601 UTC timestamp with 'Z' suffix (e.g. ``2026-06-18T10:30:00Z``)."""
        # datetime.utcnow() is deprecated in 3.12, but we use a
        # manual format to avoid the timezone-naive warning and keep
        # output stable across Python versions.
        import datetime as _dt

        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # === Internal (Phase 4.1 legacy) ===
    async def _run_all_probes(self, timeout_s: float) -> dict[str, tuple[dict[str, Any], bool]]:
        async def _run(name: str, probe: Probe) -> tuple[str, dict[str, Any], bool]:
            try:
                payload, ok = await asyncio.wait_for(probe(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return (
                    name,
                    {"status": "timeout", "timeout_s": timeout_s},
                    False,
                )
            except Exception as e:  # noqa: BLE001
                return (
                    name,
                    {"status": "error", "error": f"{type(e).__name__}: {e}"},
                    False,
                )
            return name, payload, ok

        results_list = await asyncio.gather(
            *(_run(name, probe) for name, probe in self._probes.items()),
            return_exceptions=False,
        )
        return {name: (payload, ok) for name, payload, ok in results_list}

    def _aggregate(
        self,
        results: dict[str, tuple[dict[str, Any], bool]],
        require_mode: bool,
    ) -> HealthReport:
        checks_out: dict[str, dict[str, Any]] = {}
        required_failed: list[str] = []
        any_failed: list[str] = []
        for name, (payload, ok) in results.items():
            checks_out[name] = payload
            if not ok:
                any_failed.append(name)
                if (require_mode and name == "qdrant" and self._require_qdrant) or \
                   (require_mode and name == "neo4j" and self._require_neo4j):
                    required_failed.append(name)
        if required_failed:
            status: HealthStatus = "unhealthy"
        elif any_failed:
            status = "degraded"
        else:
            status = "ok"
        return HealthReport(
            status=status,
            checks=checks_out,
            version=self._version,
            project_root=self._project_root,
        )


__all__ = [
    "HealthChecker",
    "HealthReport",
    "HealthStatus",
    "ProbeResult",
    "ProbeStatus",
]
