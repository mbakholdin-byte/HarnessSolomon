"""Phase 4.1 Step 6: Singleton observability access layer.

Process-level singleton that wires JsonlLogger + PrometheusMetrics +
OTelTracer + CostTracker into a single ``get_observability()`` accessor.

Pattern:
    - Lazy-init from Settings (avoids import-time side effects).
    - Thread-safe (double-checked locking).
    - Fail-open (any internal error → log + skip, never raise).
    - Re-uses existing instances on subsequent calls.

Public API:
    - ``get_observability()`` → ``ObservabilityHandle`` (singleton).
    - ``reset_observability()`` → for tests + hot-reload.
    - ``emit_http_request(...)``, ``emit_llm_call(...)``, ``emit_tool_call(...)``,
      ``emit_hook_dispatch(...)``, ``emit_compaction(...)``,
      ``emit_merge_queue_event(...)``, ``emit_outbound_delivery(...)``,
      ``emit_privacy_decision(...)``, ``emit_webhook_inbound(...)`` —
      high-level helpers that gate on per-event settings and call the
      underlying emit() / metric methods.

Trust boundary: this module imports only from ``harness.observability.*``
and ``harness.config``. It does NOT import from ``harness.agents``,
``harness.server``, or ``harness.hooks``. Enforced by AST test
``tests/test_observability_trust_boundary.py``.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from harness.config import Settings
from harness.observability.cost import (
    DEFAULT_COSTS,
    CostTracker,
    compute_cost,
    parse_cost_overrides,
)
from harness.observability.events import LogEvent
from harness.observability.health import HealthChecker
from harness.observability.logger import JsonlLogger
from harness.observability.metrics import PrometheusMetrics
from harness.observability.tracer import OTelTracer

_log = logging.getLogger(__name__)


@dataclass
class ObservabilityHandle:
    """Container for all observability primitives.

    Use ``get_observability()`` to obtain a process-level singleton.
    """

    settings: Settings
    logger: JsonlLogger
    metrics: PrometheusMetrics
    tracer: OTelTracer
    health: HealthChecker
    cost: CostTracker

    def emit(self, event: LogEvent) -> None:
        """Emit a structured log event. Fail-open: never raises."""
        if not self.settings.observability_enabled:
            return
        if not self.settings.observability_jsonl_enabled:
            return
        try:
            self.logger.emit(event)
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("observability.emit failed: %s", exc)

    def metric_inc(self, name: str, labels: dict[str, str] | None = None) -> None:
        """Increment a counter. Fail-open: never raises."""
        if not self.settings.observability_prometheus_enabled:
            return
        try:
            m = getattr(self.metrics, name, None)
            if m is None:
                return
            if labels:
                m.labels(**labels).inc()
            else:
                m.inc()
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("observability.metric_inc failed: %s", exc)

    def metric_observe(
        self, name: str, value: float, labels: dict[str, str] | None = None
    ) -> None:
        """Observe a histogram value. Fail-open: never raises."""
        if not self.settings.observability_prometheus_enabled:
            return
        try:
            m = getattr(self.metrics, name, None)
            if m is None:
                return
            if labels:
                m.labels(**labels).observe(value)
            else:
                m.observe(value)
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("observability.metric_observe failed: %s", exc)

    def metric_add(
        self, name: str, value: float, labels: dict[str, str] | None = None
    ) -> None:
        """Add to a counter (for cost-style cumulative). Fail-open."""
        if not self.settings.observability_prometheus_enabled:
            return
        try:
            m = getattr(self.metrics, name, None)
            if m is None:
                return
            if labels:
                m.labels(**labels).inc(value)
            else:
                m.inc(value)
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("observability.metric_add failed: %s", exc)

    def metric_set(self, name: str, value: float) -> None:
        """Set a gauge value. Fail-open."""
        if not self.settings.observability_prometheus_enabled:
            return
        try:
            m = getattr(self.metrics, name, None)
            if m is None:
                return
            m.set(value)
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("observability.metric_set failed: %s", exc)

    @contextmanager
    def span(
        self, name: str, **attributes: Any
    ) -> Iterator[Any]:
        """Context manager wrapping tracer.start_span. Fail-open."""
        try:
            with self.tracer.start_span(name, **attributes) as s:
                yield s
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("observability.span failed: %s", exc)
            yield None


_lock = threading.Lock()
_instance: ObservabilityHandle | None = None


def _build(settings: Settings) -> ObservabilityHandle:
    """Build a fresh ObservabilityHandle from settings.

    Idempotent on imports — only constructs the actual primitives that
    are enabled by the per-event flags. All other primitives are still
    created (cost is ~zero) so the singleton API surface is uniform.
    """
    log_dir: Path = settings.observability_log_dir
    logger = JsonlLogger(log_dir)
    metrics = PrometheusMetrics(namespace=settings.observability_metrics_namespace)
    tracer = OTelTracer(
        name=settings.observability_metrics_namespace,
        sample_ratio=settings.observability_trace_sample_ratio,
        otlp_endpoint=settings.observability_otlp_endpoint,
        otlp_headers=settings.observability_otlp_headers,
    )
    health = HealthChecker(version="1.7.1")
    health.configure(
        ready_timeout_s=settings.observability_health_ready_timeout_s,
        deep_timeout_s=settings.observability_health_deep_timeout_s,
        require_qdrant=settings.observability_health_require_qdrant,
        require_neo4j=settings.observability_health_require_neo4j,
    )
    cost_table = dict(DEFAULT_COSTS)
    if settings.observability_cost_overrides:
        cost_table.update(parse_cost_overrides(settings.observability_cost_overrides))
    cost = CostTracker()
    # Per-task aggregation is opt-in; compute_cost uses DEFAULT_COSTS at call time.
    # Settings.observability_cost_enabled controls whether emit_llm_call
    # computes cost at all (gate is in emit_llm_call, not here).
    return ObservabilityHandle(
        settings=settings,
        logger=logger,
        metrics=metrics,
        tracer=tracer,
        health=health,
        cost=cost,
    )


def get_observability(settings: Settings | None = None) -> ObservabilityHandle:
    """Return the process-level singleton ObservabilityHandle.

    Double-checked locking. Safe to call from any thread.
    """
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is not None:
            return _instance
        s = settings or Settings()
        _instance = _build(s)
        return _instance


def reset_observability() -> None:
    """Reset the singleton. For tests + hot-reload only."""
    global _instance
    with _lock:
        _instance = None


# === High-level helpers (used by Step 6 wiring points) ===
#
# Each helper gates on its per-event setting, so disabling an event class
# in config (e.g. observability_log_tool_calls=False) is zero-overhead.
# Helper signatures mirror the trigger points documented in
# docs/PHASE4-OBSERVABILITY-PLAN.md §17.


def _now_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 3)


def emit_http_request(
    method: str,
    route: str,
    status: int,
    duration_s: float,
    *,
    request_id: str = "",
) -> None:
    """Emit an HTTP request log + metrics. Step 6.2 wiring."""
    obs = get_observability()
    if not obs.settings.observability_log_http_requests:
        return
    obs.metric_inc(
        "http_requests_total",
        {"route": route, "method": method, "status": str(status)},
    )
    obs.metric_observe(
        "http_request_duration_seconds",
        duration_s,
        {"route": route, "method": method},
    )
    obs.emit(
        LogEvent(
            event="http_request",
            payload={"method": method, "route": route, "status": status, "duration_s": duration_s},
            request_id=request_id,
            latency_ms=round(duration_s * 1000, 3),
            status="ok" if status < 400 else "error",
        )
    )


def emit_llm_call(
    model: str,
    tier: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_s: float,
    status: str = "ok",
    *,
    error: str = "",
    request_id: str = "",
) -> float:
    """Emit an LLM call log + metrics. Step 6.3 wiring.

    Returns the computed cost_usd (0.0 if cost tracking is disabled).
    """
    obs = get_observability()
    if not obs.settings.observability_log_llm_calls:
        return 0.0
    cost_usd = (
        compute_cost(
            model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            costs=(
                parse_cost_overrides(obs.settings.observability_cost_overrides)
                if obs.settings.observability_cost_overrides
                else None
            ),
        )
        if obs.settings.observability_cost_enabled
        else 0.0
    )
    labels = {"model": model, "tier": tier, "status": status}
    obs.metric_inc("llm_calls_total", labels)
    obs.metric_observe("llm_latency_seconds", duration_s, {"model": model, "tier": tier})
    if cost_usd > 0.0:
        obs.metric_add("llm_cost_total_usd", cost_usd, {"model": model, "tier": tier})
    obs.cost.record_call(model, prompt_tokens, completion_tokens)
    obs.emit(
        LogEvent(
            event="llm_call",
            payload={
                "model": model,
                "tier": tier,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost_usd,
                "status": status,
                "error": error,
            },
            request_id=request_id,
            latency_ms=round(duration_s * 1000, 3),
            status="ok" if status == "ok" else "error",
        )
    )
    return cost_usd


def emit_tool_call(
    tool_name: str,
    duration_s: float,
    status: str = "ok",
    *,
    error: str = "",
    request_id: str = "",
) -> None:
    """Emit a tool call log + metrics. Step 6.4 wiring."""
    obs = get_observability()
    if not obs.settings.observability_log_tool_calls:
        return
    obs.metric_inc("tool_calls_total", {"tool_name": tool_name, "status": status})
    obs.metric_observe("tool_duration_seconds", duration_s, {"tool_name": tool_name})
    obs.emit(
        LogEvent(
            event="tool_call",
            payload={"tool_name": tool_name, "status": status, "error": error},
            request_id=request_id,
            latency_ms=round(duration_s * 1000, 3),
            status="ok" if status == "ok" else "error",
        )
    )


def emit_hook_dispatch(
    event: str,
    decision: str,
    duration_s: float,
    *,
    hook_name: str = "",
    request_id: str = "",
) -> None:
    """Emit a hook dispatch log + metrics. Step 6.5 wiring."""
    obs = get_observability()
    if not obs.settings.observability_log_hook_dispatches:
        return
    obs.metric_inc("hook_dispatches_total", {"event": event, "decision": decision})
    obs.metric_observe("hook_duration_seconds", duration_s, {"event": event})
    obs.emit(
        LogEvent(
            event="hook_dispatch",
            payload={"event": event, "decision": decision, "hook_name": hook_name},
            request_id=request_id,
            latency_ms=round(duration_s * 1000, 3),
        )
    )


def emit_compaction(
    mode: str,
    cache_hit: bool,
    duration_s: float,
    *,
    session_id: str = "",
) -> None:
    """Emit a compaction log + metrics. Step 6.6 wiring."""
    obs = get_observability()
    if not obs.settings.observability_log_compactions:
        return
    obs.metric_inc(
        "compaction_total",
        {"mode": mode, "cache_hit": "true" if cache_hit else "false"},
    )
    obs.metric_observe("compaction_duration_seconds", duration_s, {"mode": mode})
    obs.metric_set("last_compact_age_seconds", 0.0)
    obs.emit(
        LogEvent(
            event="compaction",
            payload={"mode": mode, "cache_hit": cache_hit, "duration_s": duration_s},
            session_id=session_id,
            latency_ms=round(duration_s * 1000, 3),
        )
    )


def emit_merge_queue_event(
    kind: str,
    status: str = "ok",
    *,
    queue_depth: int | None = None,
    job_id: str = "",
    error: str = "",
) -> None:
    """Emit a merge queue event log + metrics. Step 6.7 wiring."""
    obs = get_observability()
    if not obs.settings.observability_log_merge_queue_events:
        return
    obs.metric_inc("merge_queue_events_total", {"kind": kind, "status": status})
    if queue_depth is not None:
        obs.metric_set("queue_depth", float(queue_depth))
    obs.emit(
        LogEvent(
            event="merge_queue_event",
            payload={"kind": kind, "status": status, "job_id": job_id, "error": error},
            status="ok" if status == "ok" else "error",
        )
    )


def emit_outbound_delivery(
    kind: str,
    status_code: str,
    duration_s: float = 0.0,
    *,
    error: str = "",
    request_id: str = "",
) -> None:
    """Emit an outbound webhook delivery log + metrics. Step 6.8 wiring."""
    obs = get_observability()
    if not obs.settings.observability_log_outbound_deliveries:
        return
    obs.metric_inc(
        "outbound_deliveries_total",
        {"kind": kind, "status_code": status_code},
    )
    obs.emit(
        LogEvent(
            event="outbound_delivery",
            payload={
                "kind": kind,
                "status_code": status_code,
                "duration_s": duration_s,
                "error": error,
            },
            request_id=request_id,
            latency_ms=round(duration_s * 1000, 3),
            status="ok" if status_code.startswith("2") else "error",
        )
    )


def emit_privacy_decision(action: str, *, path: str = "", pattern: str = "") -> None:
    """Emit a privacy zone decision log + metrics. Step 6.9 wiring."""
    obs = get_observability()
    if not obs.settings.observability_log_privacy_decisions:
        return
    obs.metric_inc("privacy_zone_total", {"action": action})
    obs.emit(
        LogEvent(
            event="privacy_decision",
            payload={"action": action, "path": path, "pattern": pattern},
        )
    )


def emit_webhook_inbound(event_type: str, status: str, *, delivery_id: str = "") -> None:
    """Emit an inbound webhook log + metrics. Step 6.10 wiring."""
    obs = get_observability()
    if not obs.settings.observability_enabled:
        return
    obs.metric_inc("webhook_inbound_total", {"event_type": event_type, "status": status})
    obs.emit(
        LogEvent(
            event="webhook_inbound",
            payload={"event_type": event_type, "status": status, "delivery_id": delivery_id},
            status="ok" if status == "ok" else "error",
        )
    )


def emit_elicitation_response(
    decision: str,
    *,
    question: str = "",
    hook_name: str = "",
    request_id: str = "",
) -> None:
    """Emit an Elicitation hook decision (Phase 4.3).

    Counter ``elicitation_total`` is labeled by decision
    (``allow`` / ``modify`` / ``block``). No structured log event —
    Elicitation is interactive, payloads can contain user-supplied
    answers (PII risk); we log only the question text + hook name.
    """
    obs = get_observability()
    if not obs.settings.observability_enabled:
        return
    obs.metric_inc("elicitation_total", {"decision": decision})
    if not obs.settings.observability_jsonl_enabled:
        return
    # Truncate question to 200 chars — defensive against pathologically
    # long payloads.
    q = question[:200] if question else ""
    obs.emit(
        LogEvent(
            event="elicitation_response",
            payload={"decision": decision, "question": q, "hook_name": hook_name},
            request_id=request_id,
        )
    )


def emit_notification_dispatched(
    severity: str,
    channel: str,
    *,
    message: str = "",
    hook_name: str = "",
    request_id: str = "",
) -> None:
    """Emit a Notification hook dispatch (Phase 4.3).

    Counter ``notification_total`` is labeled by ``(severity, channel)``
    (severity ∈ info/warn/error; channel ∈ stdout/webhook/desktop).
    Notification is fire-and-forget, so no decision counter — the
    counter records what was actually dispatched, not the outcome.
    """
    obs = get_observability()
    if not obs.settings.observability_enabled:
        return
    obs.metric_inc(
        "notification_total",
        {"severity": severity, "channel": channel},
    )
    if not obs.settings.observability_jsonl_enabled:
        return
    msg = message[:200] if message else ""
    obs.emit(
        LogEvent(
            event="notification_dispatched",
            payload={
                "severity": severity,
                "channel": channel,
                "message": msg,
                "hook_name": hook_name,
            },
            request_id=request_id,
        )
    )
