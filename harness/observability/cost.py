"""Phase 4.1: CostTracker — per-task cost from token counts.

Computes LLM call cost as ``(prompt_tokens * input_cost + completion_tokens * output_cost) / 1000``.
Cost table is per-model, in USD per 1k tokens. Default covers 12 popular
models (Anthropic, OpenAI, MiniMax, GLM, Kimi, etc.). User can override
via ``observability_cost_overrides`` setting (JSON).

Trust boundary: stdlib only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# (input_cost_per_1k, output_cost_per_1k) in USD.
# Prices as of 2026-06-16 — will go stale, see R1.
DEFAULT_COSTS: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-opus": (0.015, 0.075),
    "claude-3-haiku": (0.00025, 0.00125),
    # OpenAI
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    # Open-source cloud (MiniMax, ZhipuAI, Moonshot)
    "MiniMax-M2.7": (0.001, 0.002),
    "MiniMax-M3": (0.002, 0.004),
    "glm-4.5": (0.0007, 0.0007),
    "glm-4.7": (0.001, 0.002),
    "moonshot-v1-128k": (0.001, 0.002),
    "kimi-k2.6": (0.001, 0.002),
}


def compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    costs: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute LLM call cost in USD.

    Args:
        model: Model id (must match key in ``costs`` table).
        prompt_tokens: Input token count.
        completion_tokens: Output token count.
        costs: Cost table. Default = ``DEFAULT_COSTS``.

    Returns:
        Cost in USD. 0.0 if model not in table.
    """
    table = costs or DEFAULT_COSTS
    in_cost, out_cost = table.get(model, (0.0, 0.0))
    return (prompt_tokens * in_cost + completion_tokens * out_cost) / 1000.0


@dataclass
class CostTracker:
    """Aggregates cost across multiple LLM calls.

    Thread-unsafe (use one instance per session, mirror HookRegistry).
    Use ``record_call()`` to add a call, ``total()`` for cumulative,
    ``by_model()`` for per-model breakdown.
    """

    _per_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    _total_usd: float = 0.0
    _total_calls: int = 0

    def record_call(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        costs: dict[str, tuple[float, float]] | None = None,
    ) -> float:
        """Record one LLM call, return cost in USD."""
        cost = compute_cost(model, prompt_tokens, completion_tokens, costs)
        self._total_usd += cost
        self._total_calls += 1
        if model not in self._per_model:
            self._per_model[model] = {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
            }
        m = self._per_model[model]
        m["calls"] += 1
        m["prompt_tokens"] += prompt_tokens
        m["completion_tokens"] += completion_tokens
        m["cost_usd"] += cost
        return cost

    def total(self) -> float:
        return self._total_usd

    def calls(self) -> int:
        return self._total_calls

    def by_model(self) -> dict[str, dict[str, Any]]:
        return dict(self._per_model)

    def reset(self) -> None:
        self._per_model.clear()
        self._total_usd = 0.0
        self._total_calls = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_usd": round(self._total_usd, 6),
            "total_calls": self._total_calls,
            "by_model": {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in self._per_model.items()
            },
        }


def parse_cost_overrides(overrides_str: str) -> dict[str, tuple[float, float]]:
    """Parse ``observability_cost_overrides`` JSON string.

    Returns a dict mapping model -> (input_cost, output_cost). Empty
    string returns empty dict (caller should fall back to DEFAULT_COSTS).
    """
    if not overrides_str:
        return {}
    try:
        data = json.loads(overrides_str)
        result: dict[str, tuple[float, float]] = {}
        for k, v in data.items():
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                logger.warning(
                    "parse_cost_overrides: skipping %r — expected [input, output]",
                    k,
                )
                continue
            result[k] = (float(v[0]), float(v[1]))
        return result
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("parse_cost_overrides: invalid JSON: %s", e)
        return {}


__all__ = ["CostTracker", "DEFAULT_COSTS", "compute_cost", "parse_cost_overrides"]
