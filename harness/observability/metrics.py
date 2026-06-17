"""Phase 4.1: PrometheusMetrics — counters + histograms + gauges.

Optional dependency: requires ``prometheus-client>=0.20``. If not
installed, the class falls back to a no-op implementation (zero
overhead, ``observability_prometheus_enabled=False`` is the default).

Trust boundary: this module is stdlib + optional ``prometheus_client``
only. No ``harness.agents`` / ``harness.server`` imports.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Try to import prometheus_client; gracefully degrade.
try:
    from prometheus_client import (  # type: ignore[import-untyped]
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    _HAS_PROMETHEUS = True
    _IMPORT_ERROR: str | None = None
except ImportError as e:
    _HAS_PROMETHEUS = False
    _IMPORT_ERROR = str(e)
    # Stubs for type checking only.
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"  # type: ignore[assignment]
    CollectorRegistry = None  # type: ignore[assignment,misc]
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]
    generate_latest = None  # type: ignore[assignment]


# Default histogram buckets for latency (seconds).
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class _NoOpMetric:
    """No-op stand-in for any prometheus_client metric."""

    def labels(self, **_: Any) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1.0) -> None:
        pass

    def dec(self, amount: float = 1.0) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass


class PrometheusMetrics:
    """Wrapper around prometheus_client with safe degradation.

    All 17 trigger points in ``docs/PHASE4-OBSERVABILITY-PLAN.md`` §3
    map to one of these methods. Each method is a no-op if the
    underlying metric is a ``_NoOpMetric`` (i.e. prometheus_client
    is not installed).

    Naming convention: ``<namespace>_<subsystem>_<name>_<unit>``.
    Namespace = ``observability_metrics_namespace`` (default ``harness``).
    """

    def __init__(self, namespace: str = "harness") -> None:
        self._namespace = namespace
        self._enabled = _HAS_PROMETHEUS
        if not _HAS_PROMETHEUS:
            logger.info(
                "prometheus_client not installed (%s) — metrics are no-op. "
                "Install with `pip install prometheus-client` to enable.",
                _IMPORT_ERROR,
            )
            self._registry: CollectorRegistry | None = None  # type: ignore[assignment]
            self._init_noop_metrics()
        else:
            self._registry = CollectorRegistry()
            self._init_real_metrics()

    # === Initialisation ===
    def _init_noop_metrics(self) -> None:
        for name in (
            "http_requests_total", "http_request_duration_seconds",
            "llm_calls_total", "llm_latency_seconds", "llm_cost_total_usd",
            "hook_dispatches_total", "hook_duration_seconds",
            "tool_calls_total", "tool_duration_seconds",
            "compaction_total", "compaction_duration_seconds",
            "merge_queue_events_total", "queue_depth",
            "outbound_deliveries_total",
            "privacy_zone_total",
            "webhook_inbound_total",
            "elicitation_total",
            "notification_total",
            # Phase 4.8 v1.18.0: hook rate limiter + circuit breaker.
            "hook_rate_limited_total",
            "hook_circuit_skip_total",
            # Phase 4.8 v1.18.0: Notification deadletter queue.
            "notify_dlq_total",
            "active_sessions", "last_compact_age_seconds",
        ):
            setattr(self, name, _NoOpMetric())

    def _init_real_metrics(self) -> None:
        assert self._registry is not None
        n = self._namespace
        # HTTP
        self.http_requests_total = Counter(
            f"{n}_http_requests_total",
            "Total HTTP requests received",
            ["route", "method", "status"],
            registry=self._registry,
        )
        self.http_request_duration_seconds = Histogram(
            f"{n}_http_request_duration_seconds",
            "HTTP request latency",
            ["route", "method"],
            buckets=DEFAULT_BUCKETS,
            registry=self._registry,
        )
        # LLM
        self.llm_calls_total = Counter(
            f"{n}_llm_calls_total",
            "Total LLM completion calls",
            ["model", "tier", "status"],
            registry=self._registry,
        )
        self.llm_latency_seconds = Histogram(
            f"{n}_llm_latency_seconds",
            "LLM call latency",
            ["model", "tier"],
            buckets=DEFAULT_BUCKETS,
            registry=self._registry,
        )
        self.llm_cost_total_usd = Counter(
            f"{n}_llm_cost_total_usd",
            "Cumulative LLM cost in USD",
            ["model", "tier"],
            registry=self._registry,
        )
        # Hooks
        self.hook_dispatches_total = Counter(
            f"{n}_hook_dispatches_total",
            "Total hook dispatches",
            ["event", "decision"],
            registry=self._registry,
        )
        self.hook_duration_seconds = Histogram(
            f"{n}_hook_duration_seconds",
            "Hook dispatch latency",
            ["event"],
            buckets=DEFAULT_BUCKETS,
            registry=self._registry,
        )
        # Tools
        self.tool_calls_total = Counter(
            f"{n}_tool_calls_total",
            "Total tool calls",
            ["tool_name", "status"],
            registry=self._registry,
        )
        self.tool_duration_seconds = Histogram(
            f"{n}_tool_duration_seconds",
            "Tool call latency",
            ["tool_name"],
            buckets=DEFAULT_BUCKETS,
            registry=self._registry,
        )
        # Compaction
        self.compaction_total = Counter(
            f"{n}_compaction_total",
            "Total compactions",
            ["mode", "cache_hit"],
            registry=self._registry,
        )
        self.compaction_duration_seconds = Histogram(
            f"{n}_compaction_duration_seconds",
            "Compaction latency",
            ["mode"],
            buckets=DEFAULT_BUCKETS,
            registry=self._registry,
        )
        # Merge queue
        self.merge_queue_events_total = Counter(
            f"{n}_merge_queue_events_total",
            "Total merge queue events",
            ["kind", "status"],
            registry=self._registry,
        )
        self.queue_depth = Gauge(
            f"{n}_queue_depth",
            "Current merge queue depth",
            registry=self._registry,
        )
        # Outbound
        self.outbound_deliveries_total = Counter(
            f"{n}_outbound_deliveries_total",
            "Total outbound webhook deliveries",
            ["kind", "status_code"],
            registry=self._registry,
        )
        # Privacy
        self.privacy_zone_total = Counter(
            f"{n}_privacy_zone_total",
            "Privacy zone decisions",
            ["action"],
            registry=self._registry,
        )
        # Webhook inbound
        self.webhook_inbound_total = Counter(
            f"{n}_webhook_inbound_total",
            "Total inbound webhooks received",
            ["event_type", "status"],
            registry=self._registry,
        )
        # Phase 4.3: Elicitation + Notification hook outcomes
        self.elicitation_total = Counter(
            f"{n}_elicitation_total",
            "Total Elicitation hook decisions (interactive prompts)",
            ["decision"],
            registry=self._registry,
        )
        self.notification_total = Counter(
            f"{n}_notification_total",
            "Total Notification hook dispatches (fire-and-forget push)",
            ["severity", "channel"],
            registry=self._registry,
        )
        # Phase 4.8 v1.18.0: per-hook rate limiter + circuit breaker.
        self.hook_rate_limited_total = Counter(
            f"{n}_hook_rate_limited_total",
            "Total hook dispatches skipped by rate limiter",
            ["hook_id"],
            registry=self._registry,
        )
        self.hook_circuit_skip_total = Counter(
            f"{n}_hook_circuit_skip_total",
            "Total hook dispatches skipped by circuit breaker",
            ["hook_id", "state"],
            registry=self._registry,
        )
        # Phase 4.8 v1.18.0: deadletter queue counter for failed
        # notifications. Labeled by (severity, channel, terminal).
        # ``terminal="true"`` means the payload was persisted to the
        # SQLite DLQ after exhausting retries; ``terminal="false"``
        # means a permanent error short-circuited to the DLQ without
        # any retry (e.g. HTTP 4xx, ValueError).
        self.notify_dlq_total = Counter(
            f"{n}_notify_dlq_total",
            "Total Notification deadletter entries (retries exhausted or permanent error)",
            ["severity", "channel", "terminal"],
            registry=self._registry,
        )
        # Sessions
        self.active_sessions = Gauge(
            f"{n}_active_sessions",
            "Current active sessions count",
            registry=self._registry,
        )
        self.last_compact_age_seconds = Gauge(
            f"{n}_last_compact_age_seconds",
            "Seconds since last successful compaction",
            registry=self._registry,
        )

    # === Public API ===
    @property
    def enabled(self) -> bool:
        """True if prometheus_client is installed and metrics are real."""
        return self._enabled

    def render(self) -> bytes:
        """Render metrics in Prometheus text format.

        No-op stub returns ``b""`` if prometheus_client not installed.
        """
        if not self._enabled or self._registry is None or generate_latest is None:
            return b""
        return generate_latest(self._registry)

    @property
    def content_type(self) -> str:
        """Prometheus content-type for /metrics endpoint."""
        return CONTENT_TYPE_LATEST

    def snapshot(self) -> dict[str, dict[tuple[tuple[str, str], ...], float]]:
        """Return a JSON-safe snapshot of current counter/gauge values.

        Format::

            {
                "metric_name": {
                    (("label", "value"), ...): <value>,
                },
                ...
            }

        - Histograms are skipped (they have bucket counters; the
          ``render()`` text is the canonical export).
        - Counters and gauges are read from the live ``prometheus_client``
          objects when available, falling back to parsing ``render()``.
        - No-op stubs (prometheus_client not installed) yield ``{}``.

        Used by ``harness observability stats`` (Phase 4.4 v1.13.0) so
        the CLI can render a per-counter summary without re-implementing
        the Prometheus text parser.

        Trust boundary: stdlib + ``re`` only. No external imports.
        """
        out: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        if not self._enabled:
            return out
        # Prefer introspecting the live prometheus_client objects
        # (avoids a roundtrip through ``render()``).
        for attr in dir(self):
            if attr.startswith("_"):
                continue
            metric = getattr(self, attr, None)
            if metric is None:
                continue
            cls_name = type(metric).__name__
            if cls_name not in ("Counter", "Gauge"):
                continue
            # Walk children (one per label-set). The metric
            # object's ``_metrics`` dict maps the labels tuple
            # to a ``_Value`` instance with a ``.get()`` method.
            metrics_dict = getattr(metric, "_metrics", None)
            if not isinstance(metrics_dict, dict):
                continue
            for labels, value in metrics_dict.items():
                # ``labels`` is a frozendict-like mapping; we
                # convert to a stable tuple of (k, v) pairs.
                if hasattr(labels, "items"):
                    label_items = tuple(sorted(labels.items()))
                else:
                    label_items = ()
                try:
                    v = float(value.get())
                except Exception:  # noqa: BLE001 — defensive
                    continue
                out.setdefault(attr, {})[label_items] = v
        return out


__all__ = ["PrometheusMetrics", "DEFAULT_BUCKETS", "_NoOpMetric"]
