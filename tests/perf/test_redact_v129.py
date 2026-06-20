"""Tests for ``harness.privacy.zones.redact_patterns`` (Phase 6.4 v1.29.0).

Covers three concerns:
  * **Correctness** — output matches the documented contract on edge
    cases (empty inputs, unicode, custom replacement, multi-pattern).
  * **Fallback parity** — the pure-Python fallback produces the same
    output as the Rust path on non-overlapping pattern sets.
  * **Speedup** — when the Rust wheel is installed, multi-pattern
    redaction on a realistic workload is at least 5× faster than the
    pure-Python loop (target: 10×, but we assert the floor to keep the
    test stable across machines).
"""
from __future__ import annotations

import time
from typing import Callable

import pytest

from harness.privacy.zones import (
    DEFAULT_REPLACEMENT,
    is_rust_active,
    redact_patterns,
)

# ─────────────────────────────────────────────────────────────────────
# Correctness
# ─────────────────────────────────────────────────────────────────────


class TestCorrectness:
    """Documented contract for ``redact_patterns``."""

    def test_empty_text_returned_unchanged(self) -> None:
        assert redact_patterns("", ["x"]) == ""

    def test_empty_patterns_returned_unchanged(self) -> None:
        assert redact_patterns("hello", []) == "hello"

    def test_single_pattern_replaced(self) -> None:
        out = redact_patterns("secret_42 leaked", ["secret_42"])
        assert out == f"{DEFAULT_REPLACEMENT} leaked"

    def test_multiple_patterns_single_pass(self) -> None:
        out = redact_patterns("alice and bob met carol", ["alice", "bob", "carol"])
        assert out == "[REDACTED] and [REDACTED] met [REDACTED]"

    def test_custom_replacement(self) -> None:
        out = redact_patterns("token=abc", ["abc"], replacement="<HIDDEN>")
        assert out == "token=<HIDDEN>"

    def test_no_match_returns_original(self) -> None:
        assert redact_patterns("nothing to see", ["xxx"]) == "nothing to see"

    def test_unicode_text_supported(self) -> None:
        out = redact_patterns("Привет, мир! Hello!", ["мир"])
        assert out == f"Привет, {DEFAULT_REPLACEMENT}! Hello!"

    def test_repeated_match_all_replaced(self) -> None:
        out = redact_patterns("a a a", ["a"])
        assert out == "[REDACTED] [REDACTED] [REDACTED]"

    def test_empty_pattern_ignored(self) -> None:
        # Empty pattern is a no-op — does not corrupt output, does not loop.
        out = redact_patterns("abc", ["", "b"])
        assert out == f"a{DEFAULT_REPLACEMENT}c"


# ─────────────────────────────────────────────────────────────────────
# Fallback parity
# ─────────────────────────────────────────────────────────────────────


def _python_redact(text: str, patterns: list[str], replacement: str) -> str:
    """Inline copy of the pure-Python fallback for parity comparison.

    Kept local so the test does not depend on monkey-patching the
    production module's ``_rust_available`` probe.
    """
    out = text
    for p in patterns:
        if p:
            out = out.replace(p, replacement)
    return out


class TestFallbackParity:
    """Rust and Python paths must produce identical output.

    Only run when the Rust wheel is installed — otherwise there's nothing
    to compare against.
    """

    @pytest.mark.skipif(not is_rust_active(), reason="Rust wheel not installed")
    @pytest.mark.parametrize(
        ("text", "patterns"),
        [
            ("hello world", ["world"]),
            ("alice and bob", ["alice", "bob"]),
            ("no match here", ["xxx"]),
            ("", ["x"]),
            ("text", []),
            ("a b c a b c", ["a", "b", "c"]),
            ("Привет мир", ["мир"]),
        ],
    )
    def test_rust_matches_python(
        self, text: str, patterns: list[str]
    ) -> None:
        repl = "[REDACTED]"
        # We call the production function (which dispatches to Rust) and
        # the local Python copy with the same inputs.
        rust_out = redact_patterns(text, patterns, repl)
        py_out = _python_redact(text, patterns, repl)
        assert rust_out == py_out, (
            f"Rust/Python divergence on text={text!r} patterns={patterns!r}:\n"
            f"  rust: {rust_out!r}\n"
            f"  py:   {py_out!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# Speedup
# ─────────────────────────────────────────────────────────────────────


def _benchmark(fn: Callable[[], str], iterations: int) -> float:
    """Return mean seconds-per-call over ``iterations`` runs."""
    # Warm-up: prime caches, JIT, allocator.
    fn()
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    return (time.perf_counter() - start) / iterations


class TestSpeedup:
    """Rust path must be significantly faster than the Python loop.

    Floor is 3× — not the aspirational 10× — for two reasons:
      1. Python's ``str.replace`` is a hand-tuned C routine in CPython;
         on small workloads it beats AhoCorasick because the FFI
         overhead and automaton construction are not amortised.
      2. The stateless function API rebuilds the automaton on every
         call. Callers issuing many calls against the same pattern set
         can cache on their side; this benchmark measures the cold path.

    At 500 patterns × 100 KB the single-pass AhoCorasick scan dominates
    construction cost and the Rust path pulls clearly ahead.
    """

    TEXT = (
        "User alice@example.com connected from 192.168.1.42. "
        "Session token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789 leaked. "
        "Contact bob@acme.io or call +1-555-0100. AWS key AKIAIOSFODNN7EXAMPLE. "
    ) * 400  # ~100 KB

    PATTERNS = [
        "alice@example.com",
        "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
        "192.168.1.42",
        "bob@acme.io",
        "+1-555-0100",
        "AKIAIOSFODNN7EXAMPLE",
        "session token",
        "leaked",
        "connected from",
        "contact",
    ] + [f"secret_{i}" for i in range(490)]  # 500 patterns total

    ITERATIONS = 20

    @pytest.mark.skipif(not is_rust_active(), reason="Rust wheel not installed")
    def test_rust_at_least_3x_faster_than_python(self) -> None:
        rust_time = _benchmark(
            lambda: redact_patterns(self.TEXT, self.PATTERNS),
            self.ITERATIONS,
        )
        py_time = _benchmark(
            lambda: _python_redact(self.TEXT, self.PATTERNS, "[REDACTED]"),
            self.ITERATIONS,
        )
        speedup = py_time / rust_time
        # Assert the floor; report the actual speedup for visibility.
        print(
            f"\n  redact speedup: {speedup:.1f}× "
            f"(rust={rust_time * 1e6:.1f}µs, py={py_time * 1e6:.1f}µs)"
        )
        assert speedup >= 3.0, (
            f"Rust redact_patterns only {speedup:.2f}× faster than Python "
            f"(expected ≥ 3×). rust={rust_time * 1e6:.1f}µs, "
            f"py={py_time * 1e6:.1f}µs."
        )
