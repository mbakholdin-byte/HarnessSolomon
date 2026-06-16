"""Phase 4.1: Tests for PrometheusMetrics — counters + histograms + gauges.

Tests work regardless of whether ``prometheus_client`` is installed
(graceful no-op degradation when missing).
"""
from __future__ import annotations

import pytest

from harness.observability import PrometheusMetrics


class TestPrometheusMetrics:
    """PrometheusMetrics: no-op when SDK missing, real when installed."""

    def test_init_no_crash(self) -> None:
        """Init should never raise (B4 graceful degradation)."""
        m = PrometheusMetrics(namespace="harness")
        assert m is not None

    def test_enabled_property_reflects_install(self) -> None:
        m = PrometheusMetrics()
        # Either True (prometheus_client installed) or False (no-op).
        assert isinstance(m.enabled, bool)

    def test_render_no_crash(self) -> None:
        """render() must always return bytes, never raise."""
        m = PrometheusMetrics()
        out = m.render()
        assert isinstance(out, bytes)

    def test_render_empty_when_disabled(self) -> None:
        """If prometheus_client not installed, render() returns b''."""
        import harness.observability.metrics as mod
        if not mod._HAS_PROMETHEUS:
            m = PrometheusMetrics()
            assert m.render() == b""

    def test_content_type(self) -> None:
        m = PrometheusMetrics()
        # Always returns the standard Prometheus content type.
        assert "text/plain" in m.content_type

    def test_counter_increment_no_crash(self) -> None:
        """Counter .inc() must work regardless of install status."""
        m = PrometheusMetrics()
        # Should not raise.
        m.http_requests_total.labels(route="/api/chat", method="POST", status="200").inc()
        m.llm_calls_total.labels(model="gpt-4o", tier="T3", status="ok").inc()
        m.hook_dispatches_total.labels(event="PreToolUse", decision="allow").inc()
        m.tool_calls_total.labels(tool_name="read_file", status="ok").inc()
        m.compaction_total.labels(mode="token", cache_hit="false").inc()
        m.merge_queue_events_total.labels(kind="enqueue", status="ok").inc()
        m.outbound_deliveries_total.labels(kind="merged", status_code="200").inc()
        m.privacy_zone_total.labels(action="block").inc()
        m.webhook_inbound_total.labels(event_type="pull_request", status="ok").inc()
        m.llm_cost_total_usd.labels(model="gpt-4o", tier="T3").inc(0.05)

    def test_histogram_observe_no_crash(self) -> None:
        m = PrometheusMetrics()
        m.http_request_duration_seconds.labels(route="/api/chat", method="POST").observe(0.123)
        m.llm_latency_seconds.labels(model="gpt-4o", tier="T3").observe(1.456)
        m.hook_duration_seconds.labels(event="PreToolUse").observe(0.005)
        m.tool_duration_seconds.labels(tool_name="read_file").observe(0.250)
        m.compaction_duration_seconds.labels(mode="token").observe(5.0)

    def test_gauge_set_no_crash(self) -> None:
        m = PrometheusMetrics()
        m.queue_depth.set(5)
        m.active_sessions.set(10)
        m.last_compact_age_seconds.set(120.0)
        # inc/dec work too.
        m.queue_depth.inc()
        m.queue_depth.dec()

    def test_all_metrics_accessible(self) -> None:
        """All 18 metric attributes must exist (no AttributeError)."""
        m = PrometheusMetrics()
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
            "active_sessions", "last_compact_age_seconds",
        ):
            assert hasattr(m, name), f"missing metric: {name}"

    def test_namespace_in_metric_names(self) -> None:
        """Custom namespace should appear in registered metric names."""
        m = PrometheusMetrics(namespace="myapp")
        # Namespace appears in registry output (only if prometheus installed).
        if m.enabled:
            output = m.render().decode("utf-8")
            assert "myapp_" in output
        # If no-op, nothing to check; the test just ensures init didn't crash.

    def test_noop_labels_returns_self(self) -> None:
        """_NoOpMetric.labels() must return self for chainable .inc()."""
        from harness.observability.metrics import _NoOpMetric
        m = _NoOpMetric()
        assert m.labels(foo="bar") is m
        m.inc()
        m.dec()
        m.set(1.0)
        m.observe(1.0)
