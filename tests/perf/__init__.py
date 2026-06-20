"""Tests for the optional Rust perf extensions (Phase 6.4 v1.29.0).

These tests exercise both the Rust fast path (when ``harness_perf`` is
installed) and the pure-Python fallback. The fallback tests always run;
the Rust tests are skipped when the wheel is not present.
"""
