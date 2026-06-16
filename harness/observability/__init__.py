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
]
