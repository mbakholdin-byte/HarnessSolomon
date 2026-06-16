"""Phase 4.1: Tests for OTelTracer — no-op fallback + real OTel paths."""
from __future__ import annotations

import pytest

from harness.observability import OTelTracer, NoOpTracer, NoOpSpan


class TestOTelTracer:
    """OTelTracer works whether OTel SDK is installed or not."""

    def test_init_no_crash(self) -> None:
        t = OTelTracer(name="harness")
        assert t is not None

    def test_enabled_property(self) -> None:
        t = OTelTracer()
        assert isinstance(t.enabled, bool)

    def test_start_span_yields(self) -> None:
        t = OTelTracer()
        with t.start_span("test_span") as span:
            assert span is not None
            # Should not raise even if no-op.

    def test_start_span_with_attributes(self) -> None:
        t = OTelTracer()
        with t.start_span("llm_call", model="gpt-4o", tier="T3") as span:
            span.set_attribute("latency_ms", 250.5)
            # No-op or real — must not raise.

    def test_get_current_trace_id_no_active_span(self) -> None:
        t = OTelTracer()
        # Without an active span, returns "".
        assert t.get_current_trace_id() == "" or len(t.get_current_trace_id()) == 32

    def test_get_current_span_id_no_active_span(self) -> None:
        t = OTelTracer()
        assert t.get_current_span_id() == "" or len(t.get_current_span_id()) == 16

    def test_nested_spans(self) -> None:
        t = OTelTracer()
        with t.start_span("outer") as outer:
            with t.start_span("inner") as inner:
                # Either no-op or real — must not raise.
                pass

    def test_span_record_exception_no_crash(self) -> None:
        t = OTelTracer()
        with t.start_span("x") as span:
            try:
                raise ValueError("test")
            except ValueError as e:
                span.record_exception(e)
                # No raise.

    def test_span_end_no_crash(self) -> None:
        t = OTelTracer()
        with t.start_span("x") as span:
            span.end()
            # No raise.


class TestNoOpTracer:
    """NoOpTracer: standalone (when OTelTracer.enabled=False)."""

    def test_init(self) -> None:
        t = NoOpTracer()
        assert t is not None

    def test_start_span_yields_noop(self) -> None:
        t = NoOpTracer()
        with t.start_span("any") as span:
            assert isinstance(span, NoOpSpan)
            assert span.name == "any"

    def test_get_trace_id_empty(self) -> None:
        t = NoOpTracer()
        assert t.get_current_trace_id() == ""
        assert t.get_current_span_id() == ""

    def test_noop_span_setter_noops(self) -> None:
        s = NoOpSpan(name="x")
        s.set_attribute("k", "v")
        s.set_status("ok")
        s.record_exception(ValueError("x"))
        s.end()
        # No raise.

    def test_start_span_passes_kwargs(self) -> None:
        t = NoOpTracer()
        # NoOpTracer accepts and ignores kwargs.
        with t.start_span("x", foo="bar", baz=1) as span:
            assert span.name == "x"
