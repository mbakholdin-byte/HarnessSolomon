"""Phase 4.9 v1.19.0 Task A: Per-tool latency histogram tests.

The new ``tool_duration_seconds_by_tool`` Histogram (labelled by
``tool_name``) replaces the coarse ``tool_duration_seconds`` for
per-tool breakdown analysis. The legacy aggregate histogram is kept
for backward compatibility with existing dashboards.

Test strategy
-------------
* Where possible, tests are **dual-mode**: they pass whether or not
  ``prometheus_client`` is installed (the no-op stub records nothing,
  so dual-mode tests assert only that the code paths execute without
  raising and that the JSONL log event is emitted).
* Tests that inspect real histogram state (bucket counts, samples,
  ``_metrics`` introspection, label-required errors) are gated on
  ``prometheus_client`` being importable via
  ``pytest.mark.skipif(not _HAS_PROMETHEUS, ...)``.

Trust boundary
--------------
This test file lives under ``tests/`` and may import anything.
The production boundary is enforced separately by
``tests/test_observability_trust_boundary.py``.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from harness.config import Settings
from harness.observability import (
    emit_tool_call,
    get_observability,
    reset_observability,
)
from harness.observability.metrics import PrometheusMetrics, _HAS_PROMETHEUS


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All 12 tool names that route through the runtime dispatch + emit.
#: Matches the names registered in ``harness/server/agent/runtime.py``
#: dispatch table (lines 306-369). The ``_emit_tool_call`` helper is
#: the single emit point for all 12, so verifying the helper covers
#: them transitively.
ALL_12_TOOLS: tuple[str, ...] = (
    "read_file",
    "edit_file",
    "write_file",
    "bash",
    "grep",
    "glob",
    "scratchpad_write_note",
    "scratchpad_read_notes",
    "scratchpad_plan_step",
    "scratchpad_mark_done",
    "scratchpad_l2_search",
    "scratchpad_l2_promote_to_l1",
)

#: Buckets declared on ``tool_duration_seconds_by_tool`` (must match
#: the Histogram definition in metrics.py).
EXPECTED_BUCKETS: tuple[float, ...] = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1,
    0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

_skip_no_prom = pytest.mark.skipif(
    not _HAS_PROMETHEUS,
    reason="prometheus_client not installed — requires real Histogram",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def obs_dir(tmp_path: Path) -> Path:
    """Per-test log dir + clean observability singleton."""
    reset_observability()
    yield tmp_path
    reset_observability()


def _settings_with(dir_: Path, **overrides: Any) -> Settings:
    """Build Settings with observability enabled, pointing at ``dir_``."""
    base: dict[str, Any] = {
        "observability_enabled": True,
        "observability_jsonl_enabled": True,
        "observability_prometheus_enabled": _HAS_PROMETHEUS,
        "observability_otlp_enabled": False,
        "observability_log_dir": dir_,
        "observability_metrics_namespace": "harness_test",
        "observability_cost_enabled": False,
        "observability_cost_overrides": "",
        "observability_log_http_requests": True,
        "observability_log_llm_calls": True,
        "observability_log_tool_calls": True,
        "observability_log_hook_dispatches": True,
        "observability_log_compactions": True,
        "observability_log_merge_queue_events": True,
        "observability_log_outbound_deliveries": True,
        "observability_log_privacy_decisions": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _get_child(metric: Any, tool_name: str) -> Any | None:
    """Return the child metric object for ``tool_name``.

    prometheus_client stores children in ``_metrics``, keyed by a
    tuple of label values (in declaration order). For our Histograms
    ``tool_name`` is the first label, so the key is ``(tool_name,)``.
    """
    children = getattr(metric, "_metrics", {}) or {}
    # Direct tuple lookup (most common shape).
    if (tool_name,) in children:
        return children[(tool_name,)]
    # Fallback: iterate, in case the key shape differs.
    label_names = list(getattr(metric, "_labelnames", ()) or ())
    try:
        idx = label_names.index("tool_name")
    except ValueError:
        idx = 0
    for key, child in children.items():
        vals = list(key) if isinstance(key, tuple) else [key]
        if len(vals) > idx and vals[idx] == tool_name:
            return child
    return None


def _child_count(metric: Any, tool_name: str) -> float:
    """Return the running sample ``_count`` for ``tool_name``.

    Uses the stable ``_child_samples()`` API (works across
    prometheus_client 0.x and 1.x).
    """
    child = _get_child(metric, tool_name)
    if child is None:
        return 0.0
    samples = getattr(child, "_child_samples", None)
    if samples is None:
        return 0.0
    for sample in samples():
        if sample.name == "_count":
            return float(sample.value)
    return 0.0


def _bucket_counts(metric: Any, tool_name: str) -> dict[float, float]:
    """Return ``{bucket_upper_str: cumulative_count}`` for ``tool_name``.

    Keys are the bucket upper bounds as they appear in the exposition
    format (``"0.005"``, ``"+Inf"``, etc.) so callers can assert on
    the canonical Prometheus string form without float rounding noise.
    """
    child = _get_child(metric, tool_name)
    if child is None:
        return {}
    out: dict[float, float] = {}
    samples = getattr(child, "_child_samples", None)
    if samples is None:
        return {}
    for sample in samples():
        if sample.name != "_bucket":
            continue
        le = sample.labels.get("le") if hasattr(sample.labels, "get") else None
        if le is None:
            continue
        if le == "+Inf":
            continue
        try:
            out[float(le)] = float(sample.value)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Test 1: emit with label increments the new histogram (real-mode)
# ---------------------------------------------------------------------------


@_skip_no_prom
def test_histogram_emit_with_label(obs_dir: Path) -> None:
    """``emit_tool_call(tool_name="read_file", ...)`` must observe the
    new ``tool_duration_seconds_by_tool`` Histogram under the
    ``read_file`` label."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    emit_tool_call(tool_name="read_file", duration_s=0.05, status="ok")
    # The child for ``read_file`` must exist and record count=1.
    assert _child_count(h.metrics.tool_duration_seconds_by_tool, "read_file") == 1.0


# ---------------------------------------------------------------------------
# Test 2: per-tool isolation
# ---------------------------------------------------------------------------


@_skip_no_prom
def test_histogram_per_tool_isolation(obs_dir: Path) -> None:
    """3 calls to read_file, 2 to grep must produce independent counts."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    for _ in range(3):
        emit_tool_call(tool_name="read_file", duration_s=0.01)
    for _ in range(2):
        emit_tool_call(tool_name="grep", duration_s=0.02)
    assert _child_count(h.metrics.tool_duration_seconds_by_tool, "read_file") == 3.0
    assert _child_count(h.metrics.tool_duration_seconds_by_tool, "grep") == 2.0


# ---------------------------------------------------------------------------
# Test 3: bucket distribution
# ---------------------------------------------------------------------------


@_skip_no_prom
def test_histogram_observe_distribution(obs_dir: Path) -> None:
    """Observing 0.001 / 0.05 / 1.0 must land in the correct buckets."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    # Three samples spanning the bucket range.
    emit_tool_call(tool_name="read_file", duration_s=0.001)
    emit_tool_call(tool_name="read_file", duration_s=0.05)
    emit_tool_call(tool_name="read_file", duration_s=1.0)
    counts = _bucket_counts(h.metrics.tool_duration_seconds_by_tool, "read_file")
    # Bucket bounds present.
    assert 0.001 in counts and 0.05 in counts and 1.0 in counts
    # Sample 0.001 lands in the 0.001 bucket and every higher bucket.
    assert counts[0.001] == 1.0
    # Sample 0.05 lands in the 0.05 bucket and every higher bucket.
    assert counts[0.05] == 2.0
    # Sample 1.0 lands in the 1.0 bucket and ``+Inf``.
    assert counts[1.0] == 3.0


# ---------------------------------------------------------------------------
# Test 4: observing without label raises (real-mode)
# ---------------------------------------------------------------------------


@_skip_no_prom
def test_histogram_default_no_label_crash(obs_dir: Path) -> None:
    """A labelled Histogram must raise when observed without labels."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    # Direct observe on the parent Histogram (no labels()) — ValueError
    # because the Histogram declares ``tool_name`` as a required label.
    with pytest.raises(ValueError):
        h.metrics.tool_duration_seconds_by_tool.observe(0.1)


# ---------------------------------------------------------------------------
# Test 5: emit helper swallows exceptions (fail-open)
# ---------------------------------------------------------------------------


def test_emit_helper_swallows_exceptions(
    obs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the underlying metric object raises during ``observe()``,
    ``emit_tool_call`` must not propagate — ``ObservabilityHandle.
    metric_observe`` is fail-open (try/except + debug log).

    We simulate the failure by replacing ``metrics`` with a stub whose
    ``.labels(...).observe(...)`` chain throws. This exercises the
    real fail-open path inside ``ObservabilityHandle.metric_observe``
    (not a synthetic exception on the helper itself)."""
    s = _settings_with(obs_dir, observability_prometheus_enabled=True)
    h = get_observability(s)

    class _BrokenMetric:
        def labels(self, **_kw: Any) -> "_BrokenMetric":
            return self

        def observe(self, _v: float) -> None:
            raise RuntimeError("simulated metrics failure")

        def inc(self, _a: float = 1.0) -> None:
            raise RuntimeError("simulated counter failure")

    class _BrokenMetrics:
        tool_calls_total = _BrokenMetric()  # type: ignore[assignment]
        tool_duration_seconds = _BrokenMetric()  # type: ignore[assignment]
        tool_duration_seconds_by_tool = _BrokenMetric()  # type: ignore[assignment]

    monkeypatch.setattr(h, "metrics", _BrokenMetrics())
    # Must not raise even though every metric path throws.
    emit_tool_call(tool_name="read_file", duration_s=0.1, status="ok")


# ---------------------------------------------------------------------------
# Test 6: legacy aggregate histogram still works (backward compat)
# ---------------------------------------------------------------------------


@_skip_no_prom
def test_old_aggregate_still_works(obs_dir: Path) -> None:
    """The legacy ``tool_duration_seconds`` Histogram must still be
    observed by ``emit_tool_call`` for backward compatibility."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    emit_tool_call(tool_name="bash", duration_s=0.5, status="ok")
    # Legacy histogram has its own label set (tool_name, status) —
    # check count under ``bash``.
    assert _child_count(h.metrics.tool_duration_seconds, "bash") == 1.0


# ---------------------------------------------------------------------------
# Test 7: all 12 tools emit (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", ALL_12_TOOLS)
def test_all_12_tools_have_emit(tool_name: str, obs_dir: Path) -> None:
    """Every tool name routed through the runtime dispatch must emit
    without raising and produce a JSONL ``tool_call`` event."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    emit_tool_call(tool_name=tool_name, duration_s=0.02, status="ok")
    lines = h.logger.tail(n=10)
    ev = next((e for e in lines if e.get("event") == "tool_call"), None)
    assert ev is not None, f"no tool_call event emitted for {tool_name}"
    assert ev["payload"]["tool_name"] == tool_name


# ---------------------------------------------------------------------------
# Test 8: scratchpad tools emit (subset of Test 7, focuses on the
# 6 scratchpad tools from runtime.py)
# ---------------------------------------------------------------------------


SCRATCHPAD_TOOLS: tuple[str, ...] = tuple(
    t for t in ALL_12_TOOLS if t.startswith("scratchpad_")
)


@_skip_no_prom
def test_scratchpad_tools_emit(obs_dir: Path) -> None:
    """All 6 scratchpad tools must record distinct histogram entries."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    for name in SCRATCHPAD_TOOLS:
        emit_tool_call(tool_name=name, duration_s=0.005)
    # Each scratchpad tool should have exactly one sample.
    for name in SCRATCHPAD_TOOLS:
        assert _child_count(h.metrics.tool_duration_seconds_by_tool, name) == 1.0
    # Sanity: the 6 scratchpad tools are registered.
    assert len(SCRATCHPAD_TOOLS) == 6


# ---------------------------------------------------------------------------
# Test 9: snapshot / render includes new metric
# ---------------------------------------------------------------------------


def test_metrics_snapshot_includes_new() -> None:
    """``PrometheusMetrics`` must expose ``tool_duration_seconds_by_tool``
    as an attribute (works in no-op mode too via _NoOpMetric)."""
    m = PrometheusMetrics()
    assert hasattr(m, "tool_duration_seconds_by_tool")
    # No-op or real — both support .labels().observe().
    m.tool_duration_seconds_by_tool.labels(tool_name="read_file").observe(0.1)


@_skip_no_prom
def test_render_text_contains_new_metric() -> None:
    """The Prometheus text exposition format must include the new
    metric name."""
    m = PrometheusMetrics()
    m.tool_duration_seconds_by_tool.labels(tool_name="read_file").observe(0.1)
    text = m.render().decode("utf-8")
    assert "tool_duration_seconds_by_tool" in text


# ---------------------------------------------------------------------------
# Test 10: bucket bounds cover typical tool latency range
# ---------------------------------------------------------------------------


def test_buckets_cover_typical_range() -> None:
    """Inspect the Histogram definition: bucket[0] == 0.001 and
    bucket[-2] == 10.0 (last is ``+Inf`` sentinel) — covers fast
    in-process tools (1ms) up to pathological bash commands (10s)."""
    m = PrometheusMetrics()
    if _HAS_PROMETHEUS:
        # Real Histogram exposes ``_upper_bounds`` (includes +Inf).
        bounds = list(getattr(m.tool_duration_seconds_by_tool, "_upper_bounds", []))
        finite = [float(b) for b in bounds if b != float("inf")]
        assert finite, "no finite bucket bounds found"
        assert min(finite) == 0.001, f"expected 0.001, got {min(finite)}"
        assert max(finite) == 10.0, f"expected 10.0, got {max(finite)}"
        # Also confirm the full expected set is present.
        assert tuple(finite) == EXPECTED_BUCKETS
    else:
        # No-op mode: verify the constant directly.
        assert EXPECTED_BUCKETS[0] == 0.001
        assert EXPECTED_BUCKETS[-1] == 10.0


# ---------------------------------------------------------------------------
# Test 11: concurrent emits are thread-safe
# ---------------------------------------------------------------------------


@_skip_no_prom
def test_concurrent_emit_thread_safe(obs_dir: Path) -> None:
    """100 concurrent emits across 4 threads must produce exactly 100
    samples — prometheus_client uses locks internally but we verify
    no samples are lost or double-counted."""
    s = _settings_with(obs_dir)
    h = get_observability(s)

    N_THREADS = 4
    N_PER_THREAD = 25

    def _worker() -> None:
        for _ in range(N_PER_THREAD):
            emit_tool_call(tool_name="read_file", duration_s=0.001)

    threads = [threading.Thread(target=_worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = _child_count(h.metrics.tool_duration_seconds_by_tool, "read_file")
    assert total == N_THREADS * N_PER_THREAD


# ---------------------------------------------------------------------------
# Test 12: existing callers unaffected (regression)
# ---------------------------------------------------------------------------


def test_existing_emit_tool_call_callers_unaffected(obs_dir: Path) -> None:
    """Existing ``emit_tool_call`` callers (positional signature, no
    new kwargs) must continue to work and produce the legacy
    ``tool_calls_total`` counter event."""
    s = _settings_with(obs_dir)
    h = get_observability(s)
    # Phase 4.7 signature: positional tool_name + duration_s, kw status.
    emit_tool_call("read_file", 0.1, status="ok")
    # Phase 4.1 signature: all positional.
    emit_tool_call("grep", 0.2, "ok")

    if _HAS_PROMETHEUS:
        # Counter children expose ``_child_samples`` with a single
        # Sample whose name == the metric's base name.
        counter = h.metrics.tool_calls_total
        children = getattr(counter, "_metrics", {}) or {}
        read_file_count = 0.0
        grep_count = 0.0
        for key, child in children.items():
            vals = list(key) if isinstance(key, tuple) else [key]
            # tool_calls_total is labelled (tool_name, status).
            label_names = list(getattr(counter, "_labelnames", ()) or ())
            try:
                tn_idx = label_names.index("tool_name")
            except ValueError:
                tn_idx = 0
            tool_val = vals[tn_idx] if len(vals) > tn_idx else None
            samples = list(getattr(child, "_child_samples", lambda: [])())
            value = 0.0
            for s in samples:
                # The single counter sample has no extra labels.
                if not s.labels:
                    value = float(s.value)
                    break
            if tool_val == "read_file":
                read_file_count = value
            elif tool_val == "grep":
                grep_count = value
        assert read_file_count == 1.0
        assert grep_count == 1.0

    # JSONL events emitted for both calls.
    lines = h.logger.tail(n=20)
    events = [e for e in lines if e.get("event") == "tool_call"]
    tool_names = {e["payload"]["tool_name"] for e in events}
    assert {"read_file", "grep"}.issubset(tool_names)
