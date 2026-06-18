"""Phase 4.9 Task B: per-LLM-model cost breakdown tests.

These tests cover the two new Prometheus counters introduced in
``harness/observability/metrics.py``:

  * ``llm_cost_total_usd_by_model{model_id=...}``
  * ``llm_tokens_total{model_id=..., type="input|output"}``

and the wiring of those counters through ``emit_llm_call()`` and the
LLM router. The legacy aggregate counters (``llm_calls_total``,
``llm_cost_total_usd``) are also exercised to guarantee backwards
compatibility — operators must be able to upgrade without breaking
existing dashboards.

The test environment does NOT install ``prometheus_client``, so the
``PrometheusMetrics`` instance in production builds no-op stubs that
discard every ``.inc()``. We replace the relevant metric attributes
on the live ``ObservabilityHandle`` with small accumulating stubs
(``_AccumCounter``) so the tests can read back the increments and
assert per-model isolation, token split, cost calculations, etc.

Trust boundary: the new code paths are exercised through the public
``emit_llm_call`` helper and the public ``LLMRouter`` API — no
private symbol is imported from production modules.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.config import Settings
from harness.observability import (
    emit_llm_call,
    get_observability,
    reset_observability,
)


# === Test infrastructure ===


class _AccumCounter:
    """Minimal Counter stub that records every ``.inc()`` by label-set.

    Mirrors the prometheus_client ``Counter`` surface area used by
    ``emit_llm_call``: ``.labels(**labels).inc(amount)``. Stored
    values are summed per frozen label-set so tests can assert
    "Qwen3-8B has cost 0.001, MiniMax-M2.7 has cost 0.05" without
    caring about call ordering.
    """

    def __init__(self) -> None:
        # label-key (frozenset of (k, v) tuples) -> cumulative value.
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def labels(self, **label_kwargs: str) -> "_AccumCounterChild":
        return _AccumCounterChild(self, tuple(sorted(label_kwargs.items())))

    def inc(self, amount: float = 1.0) -> None:
        # Top-level (label-less) increment uses the empty key.
        self._bump((), amount)

    def _bump(
        self,
        label_key: tuple[tuple[str, str], ...],
        amount: float,
    ) -> None:
        self._values[label_key] = self._values.get(label_key, 0.0) + float(amount)

    def get(self, **label_kwargs: str) -> float:
        """Return the cumulative value for one label-set (0.0 if absent)."""
        return self._values.get(tuple(sorted(label_kwargs.items())), 0.0)

    @property
    def total(self) -> float:
        """Sum across all label-sets. Useful for aggregate assertions."""
        return sum(self._values.values())


class _AccumCounterChild:
    """The object returned by ``_AccumCounter.labels(...)``.

    Carries a reference to the parent counter and the label key so
    ``.inc(amount)`` can route the increment to the right slot.
    """

    def __init__(self, parent: _AccumCounter, label_key: tuple[tuple[str, str], ...]) -> None:
        self._parent = parent
        self._label_key = label_key

    def inc(self, amount: float = 1.0) -> None:
        self._parent._bump(self._label_key, amount)


@pytest.fixture
def obs_handle(tmp_path: Path):
    """Build a fresh ObservabilityHandle with breakdown counters swapped for accumulators.

    Yields the handle so tests can read ``handle.metrics.llm_cost_total_usd_by_model.get(...)``
    after calling ``emit_llm_call(...)``. Resets the singleton on
    entry and exit so tests are isolated.
    """
    reset_observability()
    s = Settings(
        observability_enabled=True,
        observability_jsonl_enabled=True,
        # The legacy aggregate path (metric_inc/metric_add) is gated
        # on this flag — we need it True so the llm_calls_total /
        # llm_cost_total_usd assertions in the legacy-compat tests
        # actually see increments. prometheus_client itself is not
        # installed in the test environment, but the gate runs before
        # prometheus_client is touched, so flipping the flag is
        # sufficient.
        observability_prometheus_enabled=True,
        observability_otlp_enabled=False,
        observability_log_dir=tmp_path,
        observability_metrics_namespace="harness_test",
        observability_cost_enabled=True,
        observability_cost_overrides="",
        observability_log_llm_calls=True,
    )
    handle = get_observability(s)
    # Replace the relevant metric attributes with accumulators so
    # tests can read back the increments. prometheus_client is not
    # installed in the test environment, so these start life as
    # ``_NoOpMetric`` instances — we overwrite them directly.
    handle.metrics.llm_cost_total_usd_by_model = _AccumCounter()
    handle.metrics.llm_tokens_total = _AccumCounter()
    handle.metrics.llm_cost_total_usd = _AccumCounter()
    handle.metrics.llm_calls_total = _AccumCounter()
    yield handle
    reset_observability()


def _settings_with(dir_: Path, **overrides: object) -> Settings:
    """Build Settings with observability enabled (mirror test_observability_wiring)."""
    base: dict[str, object] = {
        "observability_enabled": True,
        "observability_jsonl_enabled": True,
        # See the obs_handle fixture comment: the legacy metric_inc /
        # metric_add helpers gate on this flag, so we enable it to
        # exercise both the breakdown (direct metric writes) and the
        # legacy aggregate (gated writes) paths in the same test.
        "observability_prometheus_enabled": True,
        "observability_otlp_enabled": False,
        "observability_log_dir": dir_,
        "observability_metrics_namespace": "harness_test",
        "observability_cost_enabled": True,
        "observability_cost_overrides": "",
        "observability_log_llm_calls": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# === Tests ===


class TestEmitLlmCallBreakdown:
    """Direct tests of the breakdown counters in emit_llm_call()."""

    def test_emit_with_model_id_label(self, obs_handle) -> None:
        """emit_llm_call(model_id=...) routes cost into llm_cost_total_usd_by_model."""
        emit_llm_call(
            model="MiniMax-M2.7",
            tier="T3",
            prompt_tokens=1000,
            completion_tokens=500,
            duration_s=0.5,
            status="ok",
            model_id="MiniMax-M2.7",
            cost_usd_override=0.05,
        )
        counter: _AccumCounter = obs_handle.metrics.llm_cost_total_usd_by_model
        assert counter.get(model_id="MiniMax-M2.7") == pytest.approx(0.05)

    def test_emit_per_model_isolation(self, obs_handle) -> None:
        """3 calls on Qwen3-8B and 2 on MiniMax-M2.7 land in disjoint label slots."""
        for _ in range(3):
            emit_llm_call(
                model="qwen3:8b", tier="T1",
                prompt_tokens=100, completion_tokens=50, duration_s=0.1,
                model_id="qwen3:8b", cost_usd_override=0.001,
            )
        for _ in range(2):
            emit_llm_call(
                model="MiniMax-M2.7", tier="T3",
                prompt_tokens=200, completion_tokens=100, duration_s=0.2,
                model_id="MiniMax-M2.7", cost_usd_override=0.05,
            )
        counter: _AccumCounter = obs_handle.metrics.llm_cost_total_usd_by_model
        assert counter.get(model_id="qwen3:8b") == pytest.approx(0.003)
        assert counter.get(model_id="MiniMax-M2.7") == pytest.approx(0.10)
        # No cross-contamination: total equals the sum of the two models.
        assert counter.total == pytest.approx(0.103)

    def test_emit_tokens_input_output_split(self, obs_handle) -> None:
        """input=100, output=50 → llm_tokens_total splits into two type slots."""
        emit_llm_call(
            model="qwen3:8b", tier="T1",
            prompt_tokens=100, completion_tokens=50, duration_s=0.1,
            model_id="qwen3:8b", cost_usd_override=0.0,
        )
        tokens: _AccumCounter = obs_handle.metrics.llm_tokens_total
        assert tokens.get(model_id="qwen3:8b", type="input") == 100
        assert tokens.get(model_id="qwen3:8b", type="output") == 50
        # No other type slots populated for this model.
        assert tokens.total == 150

    def test_emit_zero_cost_works(self, obs_handle) -> None:
        """cost_usd=0.0 (free tier) still emits the breakdown counters."""
        emit_llm_call(
            model="qwen3:8b", tier="T1",
            prompt_tokens=10, completion_tokens=5, duration_s=0.1,
            model_id="qwen3:8b", cost_usd_override=0.0,
        )
        cost_counter: _AccumCounter = obs_handle.metrics.llm_cost_total_usd_by_model
        tokens: _AccumCounter = obs_handle.metrics.llm_tokens_total
        # Zero cost is still recorded (the counter exists, value is 0.0).
        assert cost_counter.get(model_id="qwen3:8b") == 0.0
        # Tokens are recorded even when cost is zero.
        assert tokens.get(model_id="qwen3:8b", type="input") == 10
        assert tokens.get(model_id="qwen3:8b", type="output") == 5

    def test_emit_swallows_exceptions(self, tmp_path: Path) -> None:
        """A broken metric object must NOT propagate out of emit_llm_call.

        Observability is best-effort: the call path (router, agent
        loop) must keep running even if a Counter raises. This test
        installs a counter whose ``.labels()`` raises and asserts the
        helper returns normally and emits the legacy log event.
        """
        reset_observability()
        s = _settings_with(tmp_path)
        handle = get_observability(s)

        class _BrokenCounter:
            def labels(self, **_: str) -> Any:
                raise RuntimeError("simulated prometheus failure")

        handle.metrics.llm_cost_total_usd_by_model = _BrokenCounter()  # type: ignore[assignment]
        try:
            # Should not raise — breakdown failure is logged at DEBUG.
            cost = emit_llm_call(
                model="qwen3:8b", tier="T1",
                prompt_tokens=10, completion_tokens=5, duration_s=0.1,
                model_id="qwen3:8b", cost_usd_override=0.001,
            )
            assert cost == pytest.approx(0.001)
            # The legacy JSONL event should still be emitted.
            lines = handle.logger.tail(n=10)
            assert any(ev["event"] == "llm_call" for ev in lines)
        finally:
            reset_observability()

    def test_old_aggregate_still_works(self, obs_handle) -> None:
        """Calling emit_llm_call WITHOUT model_id keeps the legacy path working.

        Pre-Phase-4.9 callers (and the old wiring tests) do not pass
        ``model_id`` — the breakdown counters must not fire, but the
        aggregate ``llm_calls_total`` and ``llm_cost_total_usd``
        counters must still increment.
        """
        cost = emit_llm_call(
            model="gpt-4o", tier="T3",
            prompt_tokens=1000, completion_tokens=500, duration_s=0.5,
        )
        assert cost > 0.0  # computed from DEFAULT_COSTS
        # Legacy aggregate counter incremented.
        calls_counter: _AccumCounter = obs_handle.metrics.llm_calls_total
        assert calls_counter.get(model="gpt-4o", tier="T3", status="ok") == 1.0
        cost_counter: _AccumCounter = obs_handle.metrics.llm_cost_total_usd
        assert cost_counter.get(model="gpt-4o", tier="T3") == pytest.approx(cost)
        # No breakdown emitted when model_id is absent.
        breakdown: _AccumCounter = obs_handle.metrics.llm_cost_total_usd_by_model
        assert breakdown.total == 0.0


class TestRouterWiring:
    """The router passes model_id + cost_usd_override into emit_llm_call."""

    async def test_router_emits_on_success_path(self, tmp_path: Path) -> None:
        """A successful router.completion() emits the breakdown with the right model_id."""
        reset_observability()
        s = _settings_with(tmp_path)
        handle = get_observability(s)
        # Swap the breakdown counters for accumulators.
        handle.metrics.llm_cost_total_usd_by_model = _AccumCounter()
        handle.metrics.llm_tokens_total = _AccumCounter()
        try:
            from harness.server.llm.router import LLMRouter

            usage = MagicMock()
            usage.prompt_tokens = 1000
            usage.completion_tokens = 500
            usage.total_tokens = 1500
            choice = MagicMock()
            choice.message.content = "ok"
            choice.message.tool_calls = None
            response = MagicMock()
            response.choices = [choice]
            response.usage = usage

            with patch("harness.server.llm.router.litellm") as mock_litellm:
                mock_litellm.completion = AsyncMock(return_value=response)
                router = LLMRouter()
                await router.completion(
                    messages=[{"role": "user", "content": "x"}],
                    model="MiniMax-M2.7",
                )
            breakdown: _AccumCounter = handle.metrics.llm_cost_total_usd_by_model
            tokens: _AccumCounter = handle.metrics.llm_tokens_total
            # The router used model_id="MiniMax-M2.7" and forwarded
            # the catalog-computed cost (1000 input @ 0.30/M +
            # 500 output @ 0.60/M = 0.0006).
            assert breakdown.get(model_id="MiniMax-M2.7") == pytest.approx(0.0006)
            assert tokens.get(model_id="MiniMax-M2.7", type="input") == 1000
            assert tokens.get(model_id="MiniMax-M2.7", type="output") == 500
        finally:
            reset_observability()

    async def test_router_emits_on_error_path(self, tmp_path: Path) -> None:
        """A failing router.completion() emits the breakdown with cost=0, status=error."""
        reset_observability()
        s = _settings_with(tmp_path)
        handle = get_observability(s)
        handle.metrics.llm_cost_total_usd_by_model = _AccumCounter()
        handle.metrics.llm_tokens_total = _AccumCounter()
        handle.metrics.llm_calls_total = _AccumCounter()
        try:
            from harness.server.llm.router import LLMRouter

            with patch("harness.server.llm.router.litellm") as mock_litellm:
                mock_litellm.completion = AsyncMock(side_effect=RuntimeError("upstream 5xx"))
                router = LLMRouter()
                with pytest.raises(RuntimeError, match="upstream 5xx"):
                    await router.completion(
                        messages=[{"role": "user", "content": "x"}],
                        model="MiniMax-M2.7",
                    )
            breakdown: _AccumCounter = handle.metrics.llm_cost_total_usd_by_model
            tokens: _AccumCounter = handle.metrics.llm_tokens_total
            calls: _AccumCounter = handle.metrics.llm_calls_total
            # Error path still emits the breakdown — with zero cost
            # and zero tokens, but non-zero call count.
            assert breakdown.get(model_id="MiniMax-M2.7") == 0.0
            assert tokens.get(model_id="MiniMax-M2.7", type="input") == 0
            assert tokens.get(model_id="MiniMax-M2.7", type="output") == 0
            assert calls.get(
                model="MiniMax-M2.7", tier="T3", status="error"
            ) == 1.0
        finally:
            reset_observability()


class TestCostCalculationLocalPricing:
    """The local pricing table (DEFAULT_COSTS) drives cost computation."""

    def test_cost_calculation_local_pricing(self) -> None:
        """Known catalog entries produce expected cost_per_token values."""
        from harness.observability.cost import DEFAULT_COSTS, compute_cost

        # Qwen3-8B is not in DEFAULT_COSTS (it's a free local model
        # in the catalog with pricing_input=0.0). compute_cost
        # returns 0.0 for unknown models — verifying the "free tier"
        # behavior relied upon by emit_llm_call's fallback path.
        assert compute_cost("qwen3:8b", 1000, 500) == 0.0
        # gpt-4o: 0.0025/1k input, 0.01/1k output.
        # 1000 * 0.0025 / 1000 + 500 * 0.01 / 1000 = 0.0025 + 0.005 = 0.0075.
        assert compute_cost("gpt-4o", 1000, 500) == pytest.approx(0.0075)
        # MiniMax-M2.7: 0.001/1k input, 0.002/1k output.
        assert compute_cost("MiniMax-M2.7", 1000, 500) == pytest.approx(0.002)
        # Sanity: every DEFAULT_COSTS entry has non-negative rates.
        for model_id, (in_cost, out_cost) in DEFAULT_COSTS.items():
            assert in_cost >= 0.0, f"{model_id}: negative input cost"
            assert out_cost >= 0.0, f"{model_id}: negative output cost"


class TestMetricsSnapshot:
    """PrometheusMetrics.snapshot() exposes the new counters."""

    def test_metrics_snapshot_includes_new(self) -> None:
        """After incrementing, snapshot() should be able to find the new metrics.

        prometheus_client is not installed in this environment, so
        ``snapshot()`` returns ``{}``. The contract we test here is
        that the new metric attributes EXIST on PrometheusMetrics
        instances and are accessible — i.e. they are not
        AttributeError-prone typos that would only surface in
        production (where prometheus_client IS installed).
        """
        from harness.observability.metrics import PrometheusMetrics

        m = PrometheusMetrics()
        # Both new attributes must exist (no-op or real).
        assert hasattr(m, "llm_cost_total_usd_by_model")
        assert hasattr(m, "llm_tokens_total")
        # The snapshot() method must not raise on a fresh instance.
        snap = m.snapshot()
        assert isinstance(snap, dict)
        # When prometheus_client IS installed and at least one label
        # has been incremented, snapshot() will surface the metric.
        # In the no-op environment, snapshot() yields {} — that's
        # the documented contract, we just verify it does not crash.
        m.llm_cost_total_usd_by_model.labels(model_id="probe").inc(0.001)
        m.llm_tokens_total.labels(model_id="probe", type="input").inc(10)
        snap2 = m.snapshot()
        assert isinstance(snap2, dict)


class TestTrustBoundary:
    """The new code does not break the observability trust boundary."""

    def test_observability_still_no_agents_server_imports(self) -> None:
        """Re-run the trust boundary check after the Phase 4.9 edits.

        This is a defensive guard: the new ``emit_llm_call`` signature
        added a ``model_id`` parameter but no new imports. The AST
        scan in ``tests/test_observability_trust_boundary.py`` is the
        canonical check; we re-assert the result here so a regression
        in this file is caught by both modules.
        """
        import ast

        observability_dir = (
            Path(__file__).resolve().parent.parent / "harness" / "observability"
        )
        forbidden = {"harness.agents", "harness.server", "harness.hooks"}
        violations: list[str] = []
        for path in observability_dir.rglob("*.py"):
            if path.suffix != ".py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                target = None
                if isinstance(node, ast.Import):
                    target = node.names[0].name if node.names else None
                elif isinstance(node, ast.ImportFrom):
                    target = node.module
                if target and target.startswith("harness."):
                    parts = target.split(".")
                    if len(parts) >= 2 and f"{parts[0]}.{parts[1]}" in forbidden:
                        violations.append(f"{path.name}:{node.lineno}: {target}")
        assert not violations, "Trust boundary violation: " + ", ".join(violations)
