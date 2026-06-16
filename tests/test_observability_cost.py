"""Phase 4.1: Tests for CostTracker + compute_cost + parse_cost_overrides."""
from __future__ import annotations

import pytest

from harness.observability import CostTracker, DEFAULT_COSTS, compute_cost
from harness.observability.cost import parse_cost_overrides


class TestComputeCost:
    """compute_cost: token × cost table → USD."""

    def test_known_model(self) -> None:
        # gpt-4o: input=0.0025/1k, output=0.01/1k
        cost = compute_cost("gpt-4o", prompt_tokens=1000, completion_tokens=500)
        # 1.0 * 0.0025 + 0.5 * 0.01 = 0.0025 + 0.005 = 0.0075
        assert abs(cost - 0.0075) < 1e-6

    def test_unknown_model_zero(self) -> None:
        cost = compute_cost("unknown-model-xyz", prompt_tokens=1000, completion_tokens=500)
        assert cost == 0.0

    def test_zero_tokens(self) -> None:
        cost = compute_cost("gpt-4o", prompt_tokens=0, completion_tokens=0)
        assert cost == 0.0

    def test_anthropic_model(self) -> None:
        # claude-3-5-sonnet: input=0.003/1k, output=0.015/1k
        cost = compute_cost(
            "claude-3-5-sonnet", prompt_tokens=2000, completion_tokens=1000
        )
        # 2.0 * 0.003 + 1.0 * 0.015 = 0.006 + 0.015 = 0.021
        assert abs(cost - 0.021) < 1e-6

    def test_custom_cost_table(self) -> None:
        custom = {"my-model": (0.5, 1.0)}
        cost = compute_cost("my-model", 1000, 1000, costs=custom)
        # 1.0 * 0.5 + 1.0 * 1.0 = 1.5
        assert abs(cost - 1.5) < 1e-6

    def test_default_costs_has_12_models(self) -> None:
        """R1 mitigation: known models covered."""
        assert len(DEFAULT_COSTS) >= 12
        for model in [
            "claude-3-5-sonnet", "claude-3-opus", "claude-3-haiku",
            "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
            "MiniMax-M2.7", "MiniMax-M3",
            "glm-4.5", "glm-4.7",
            "moonshot-v1-128k", "kimi-k2.6",
        ]:
            assert model in DEFAULT_COSTS


class TestCostTracker:
    """CostTracker: aggregation, by_model, reset, to_dict."""

    def test_empty(self) -> None:
        ct = CostTracker()
        assert ct.total() == 0.0
        assert ct.calls() == 0
        assert ct.by_model() == {}

    def test_record_call(self) -> None:
        ct = CostTracker()
        cost = ct.record_call("gpt-4o", 1000, 500)
        assert cost > 0
        assert ct.total() > 0
        assert ct.calls() == 1

    def test_aggregate_multiple(self) -> None:
        ct = CostTracker()
        ct.record_call("gpt-4o", 1000, 500)
        ct.record_call("claude-3-5-sonnet", 2000, 1000)
        ct.record_call("gpt-4o", 500, 250)
        assert ct.calls() == 3
        # 2 gpt-4o calls + 1 claude.
        by_model = ct.by_model()
        assert "gpt-4o" in by_model
        assert "claude-3-5-sonnet" in by_model
        assert by_model["gpt-4o"]["calls"] == 2
        assert by_model["gpt-4o"]["prompt_tokens"] == 1500
        assert by_model["gpt-4o"]["completion_tokens"] == 750

    def test_reset(self) -> None:
        ct = CostTracker()
        ct.record_call("gpt-4o", 1000, 500)
        assert ct.calls() == 1
        ct.reset()
        assert ct.calls() == 0
        assert ct.total() == 0.0
        assert ct.by_model() == {}

    def test_to_dict(self) -> None:
        ct = CostTracker()
        ct.record_call("gpt-4o", 1000, 500)
        d = ct.to_dict()
        assert "total_usd" in d
        assert "total_calls" in d
        assert "by_model" in d
        assert d["total_calls"] == 1
        # Cost rounded to 6 decimal places.
        assert isinstance(d["total_usd"], float)


class TestParseCostOverrides:
    """parse_cost_overrides: JSON string → cost table."""

    def test_empty_returns_empty(self) -> None:
        assert parse_cost_overrides("") == {}

    def test_valid_json(self) -> None:
        overrides = '{"gpt-4o": [3.00, 12.00], "my-model": [0.5, 1.0]}'
        result = parse_cost_overrides(overrides)
        assert result == {"gpt-4o": (3.0, 12.0), "my-model": (0.5, 1.0)}

    def test_invalid_json_returns_empty(self) -> None:
        """Invalid JSON → empty (with warning), don't crash."""
        result = parse_cost_overrides("not-json{")
        assert result == {}

    def test_wrong_shape_returns_empty(self) -> None:
        """Wrong shape (string instead of list) → empty."""
        result = parse_cost_overrides('{"gpt-4o": "not-a-list"}')
        assert result == {}

    def test_wrong_list_length_returns_empty(self) -> None:
        """List of length != 2 → empty."""
        result = parse_cost_overrides('{"gpt-4o": [3.0]}')
        assert result == {}
