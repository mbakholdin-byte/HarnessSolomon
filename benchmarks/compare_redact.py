"""Benchmark: Rust AhoCorasick vs Python str.replace for multi-pattern redaction.

Standalone script (no pytest). Run from the harness root:

    python benchmarks/compare_redact.py

Prints a table of (workload, rust_time, python_time, speedup) and exits.
The script is intentionally minimal — it is a developer-facing smoke
benchmark, not part of the test suite (``tests/perf/`` covers that with
assertions).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure the harness package is importable when run as a bare script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.privacy.zones import is_rust_active, redact_patterns  # noqa: E402


def _python_redact(text: str, patterns: list[str], replacement: str) -> str:
    out = text
    for p in patterns:
        if p:
            out = out.replace(p, replacement)
    return out


def _bench(fn, iterations: int) -> float:
    fn()  # warm-up
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    return (time.perf_counter() - start) / iterations


def _make_text(kb: int) -> str:
    base = (
        "User alice@example.com connected from 192.168.1.42. "
        "Session token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789 leaked. "
    )
    reps = max(1, kb * 1024 // len(base) + 1)
    return base * reps


def _make_patterns(n: int) -> list[str]:
    base = [
        "alice@example.com",
        "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
        "192.168.1.42",
        "session token",
        "leaked",
    ]
    return base + [f"secret_{i}" for i in range(max(0, n - len(base)))]


def main() -> int:
    rust = is_rust_active()
    print("=" * 64)
    print("  compare_redact.py — Rust AhoCorasick vs Python str.replace")
    print("=" * 64)
    print(f"  Rust fast path: {'ACTIVE' if rust else 'NOT INSTALLED'}")
    if not rust:
        print("  (Only Python timings will be reported.)")
    print()

    # (label, text_kb, n_patterns, iterations)
    workloads = [
        ("tiny    (1 KB,   10 pat)",    1,   10, 500),
        ("small   (10 KB,  50 pat)",   10,   50, 200),
        ("medium  (50 KB, 200 pat)",   50,  200,  50),
        ("large   (100 KB, 500 pat)", 100,  500,  20),
        ("xlarge  (500 KB, 1000 pat)",500, 1000,   5),
    ]

    header = f"  {'workload':<28} {'rust (µs)':>12} {'python (µs)':>14} {'speedup':>10}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label, kb, n_pat, iters in workloads:
        text = _make_text(kb)
        patterns = _make_patterns(n_pat)
        py = _bench(lambda: _python_redact(text, patterns, "[REDACTED]"), iters)
        if rust:
            ru = _bench(lambda: redact_patterns(text, patterns), iters)
            speedup = py / ru
            speed_str = f"{speedup:>8.1f}×"
            rust_str = f"{ru * 1e6:>12.1f}"
        else:
            speed_str = "      n/a"
            rust_str = "         —"
        print(
            f"  {label:<28} {rust_str:>12} {py * 1e6:>14.1f} {speed_str:>10}"
        )
    print()
    print("  Note: Python ``str.replace`` is a hand-tuned C routine; on tiny")
    print("  workloads the FFI + automaton construction overhead can make the")
    print("  Rust path slower. AhoCorasick wins decisively once the workload")
    print("  is large enough to amortise the setup cost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
