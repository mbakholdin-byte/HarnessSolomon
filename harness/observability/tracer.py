"""Phase 4.1: OTelTracer — OpenTelemetry-compatible spans.

Optional dependency: requires ``opentelemetry-api>=1.24`` + OTel SDK
extras. If not installed, falls back to ``NoOpTracer`` (zero overhead).
W3C trace context propagation (``traceparent`` header) for cross-service
correlation.

Trust boundary: stdlib + optional OTel only. No ``harness.agents`` /
``harness.server`` imports.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Try to import OTel API; gracefully degrade.
try:
    from opentelemetry import trace  # type: ignore[import-untyped]
    from opentelemetry.trace import Status, StatusCode  # type: ignore[import-untyped]
    _HAS_OTEL = True
except ImportError as e:
    _HAS_OTEL = False
    _IMPORT_ERROR = str(e)
    trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]


class NoOpTracer:
    """No-op tracer used when OTel SDK is not installed.

    All methods are no-ops. Used as default for
    ``observability_otlp_enabled=False`` (no overhead).
    """

    @contextmanager
    def start_span(self, name: str, **_: Any) -> Iterator["NoOpSpan"]:
        yield NoOpSpan(name=name)

    def get_current_trace_id(self) -> str:
        return ""

    def get_current_span_id(self) -> str:
        return ""


class NoOpSpan:
    """No-op span. All attribute setters are no-ops."""

    def __init__(self, name: str) -> None:
        self.name = name

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def end(self) -> None:
        pass


class OTelTracer:
    """OpenTelemetry tracer wrapper.

    If OTel SDK is installed, uses ``opentelemetry.trace.get_tracer()``.
    If not, returns a ``NoOpTracer`` instance (transparent fallback).

    Example::

        tracer = OTelTracer(name="harness")
        with tracer.start_span("llm_call") as span:
            span.set_attribute("model", "gpt-4o")
            ...
    """

    def __init__(self, name: str = "harness", **kwargs: Any) -> None:
        self._name = name
        if not _HAS_OTEL:
            logger.info(
                "opentelemetry-api not installed (%s) — tracer is no-op. "
                "Install with `pip install opentelemetry-api opentelemetry-sdk`.",
                _IMPORT_ERROR,
            )
            self._otel_tracer: Any = None
        else:
            assert trace is not None
            self._otel_tracer = trace.get_tracer(name)

    @property
    def enabled(self) -> bool:
        """True if OTel SDK is installed and tracer is real."""
        return _HAS_OTEL and self._otel_tracer is not None

    @contextmanager
    def start_span(self, name: str, **attrs: Any) -> Iterator[Any]:
        """Start a new span (no-op if OTel not installed)."""
        if not self.enabled:
            yield NoOpSpan(name=name)
            return
        with self._otel_tracer.start_as_current_span(name) as span:
            for k, v in attrs.items():
                span.set_attribute(k, v)
            yield span

    def get_current_trace_id(self) -> str:
        """Return current trace_id as 32-char hex (or "" if no active span)."""
        if not self.enabled or trace is None:
            return ""
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx or not ctx.is_valid:
            return ""
        return format(ctx.trace_id, "032x")

    def get_current_span_id(self) -> str:
        """Return current span_id as 16-char hex (or "" if no active span)."""
        if not self.enabled or trace is None:
            return ""
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx or not ctx.is_valid:
            return ""
        return format(ctx.span_id, "016x")


__all__ = ["OTelTracer", "NoOpTracer", "NoOpSpan"]
