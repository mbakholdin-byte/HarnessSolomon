"""Phase 4.1: Observability framework — public API.

Production observability для Solomon Harness. Mirrors ``harness.hooks/``
structure: stdlib + optional deps. Trust boundary: this package does NOT
import from ``harness.agents``, ``harness.server``, or ``harness.hooks``.
The boundary is enforced by ``tests/test_observability_trust_boundary.py``.

Public API surface:
    - ``JsonlLogger`` — structured JSONL writer (Phase 4.1 Step 2).
    - ``PrometheusMetrics`` — counters + histograms + gauges (Step 3).
    - ``OTelTracer`` — OpenTelemetry-compatible spans (Step 4).
    - ``HealthChecker`` — liveness / readiness / deep probes (Step 5).
    - ``CostTracker`` — per-task cost from token counts (Step 7).
    - ``LogEvent`` — structured log payload (frozen dataclass).
"""
from __future__ import annotations

from harness.observability.events import LogEvent
from harness.observability.cost import CostTracker, DEFAULT_COSTS, compute_cost
from harness.observability.emit import (
    ObservabilityHandle,
    emit_compaction,
    emit_elicitation_response,
    emit_hook_circuit_skip,
    emit_hook_dispatch,
    emit_hook_rate_limited,
    emit_http_request,
    emit_llm_call,
    emit_merge_queue_event,
    emit_notification_dispatched,
    emit_outbound_delivery,
    emit_privacy_decision,
    emit_tool_call,
    emit_webhook_inbound,
    get_observability,
    reset_observability,
)
from harness.observability.health import HealthChecker, HealthReport, HealthStatus
from harness.observability.logger import JsonlLogger
from harness.observability.metrics import PrometheusMetrics
from harness.observability.tracer import NoOpSpan, NoOpTracer, OTelTracer

__all__ = [
    # Data model
    "LogEvent",
    # Logger
    "JsonlLogger",
    # Metrics
    "PrometheusMetrics",
    # Tracer
    "OTelTracer",
    "NoOpTracer",
    "NoOpSpan",
    # Health
    "HealthChecker",
    "HealthReport",
    "HealthStatus",
    # Cost
    "CostTracker",
    "DEFAULT_COSTS",
    "compute_cost",
    # Singleton + emit helpers (Phase 4.1 Step 6)
    "ObservabilityHandle",
    "get_observability",
    "reset_observability",
    "emit_http_request",
    "emit_llm_call",
    "emit_tool_call",
    "emit_hook_dispatch",
    "emit_compaction",
    "emit_merge_queue_event",
    "emit_outbound_delivery",
    "emit_privacy_decision",
    "emit_webhook_inbound",
    # Phase 4.8 v1.18.0: hook rate limiter + circuit breaker metrics
    "emit_hook_rate_limited",
    "emit_hook_circuit_skip",
]
