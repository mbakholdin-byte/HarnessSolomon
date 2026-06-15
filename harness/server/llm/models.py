"""Solomon Harness — model catalog.

Single source of truth for supported LLM models, their providers,
context windows, pricing, and which env var controls their API key.

`available` is computed dynamically from the environment — a model is
`available: True` only when its env var is set to a non-empty value.
"""
from __future__ import annotations

import os

from pydantic import BaseModel

# === Catalog ===

#: Per-model cap on tools sent in a single LLM request.
#:
#: Empirically verified 2026-06-14 via live litellm calls:
#: MiniMax-M2.7 accepts at least 32 tools when tool schemas are wrapped
#: in OpenAI shape (the original 2013 error was the missing wrap, not
#: the count). Other Chinese cloud providers (zhipuai, moonshot) follow
#: similar limits. We use 16 as a comfortable default that fits the
#: common 6-12 tool Phase 0/0.5 set with headroom for growth.
#:
#: When a tool list is truncated, ``LLMRouter._limit_tools_for_model``
#: emits a warning AND increments an in-process counter. Phase 4
#: (observability) will move the counter to Prometheus.
#:
#: Reference points for future models:
#:   - OpenAI o1/o3:           128 (per OpenAI docs)
#:   - OpenAI gpt-4o/4.1:      128
#:   - Anthropic Claude 4.x:  no native limit (token-budgeted)
MODELS: list[dict] = [
    {
        "id": "qwen3:8b",
        "provider": "ollama",
        "tier": "T1",
        "env": "OLLAMA_HOST",
        "ctx": 32768,
        "pricing_input": 0.0,    # local model — free
        "pricing_output": 0.0,
        "max_tools": 16,
    },
    {
        "id": "MiniMax-M2.7",
        "provider": "minimax",
        "tier": "T3",
        "env": "MINIMAX_API_KEY",
        "ctx": 200000,
        "pricing_input": 0.30,   # $ per 1M tokens
        "pricing_output": 0.60,
        "max_tools": 16,
    },
    {
        "id": "glm-4.7",
        "provider": "zhipuai",
        "tier": "T3",
        "env": "ZHIPUAI_API_KEY",
        "ctx": 128000,
        "pricing_input": 0.10,
        "pricing_output": 0.10,
        "max_tools": 16,
    },
    {
        "id": "moonshot-v1-128k",
        "provider": "moonshot",
        "tier": "T3",
        "env": "MOONSHOT_API_KEY",
        "ctx": 128000,
        "pricing_input": 0.20,
        "pricing_output": 0.20,
        "max_tools": 16,
    },
]

#: Default cap when a model is unknown or doesn't specify max_tools.
#: 16 is a safe cross-provider default that fits the current Phase 0/0.5
#: toolset with headroom. Bump per-model if a specific provider advertises
#: a higher limit and you've verified it via live calls.
DEFAULT_MAX_TOOLS: int = 16


# === Schemas ===

class ModelSpec(BaseModel):
    """Public model spec exposed via /api/models."""

    id: str
    provider: str
    tier: str
    env: str
    ctx: int
    pricing_input: float
    pricing_output: float
    max_tools: int = DEFAULT_MAX_TOOLS
    available: bool = False  # computed from env at construction time


# === Helpers ===

def _is_available(env_var: str) -> bool:
    """True iff env_var is set to a non-empty string."""
    return bool(os.environ.get(env_var, "").strip())


def _build_spec(entry: dict) -> ModelSpec:
    """Build a ModelSpec from a MODELS catalog entry, with safe defaults."""
    return ModelSpec(
        id=entry["id"],
        provider=entry["provider"],
        tier=entry["tier"],
        env=entry["env"],
        ctx=entry["ctx"],
        pricing_input=entry["pricing_input"],
        pricing_output=entry["pricing_output"],
        max_tools=entry.get("max_tools", DEFAULT_MAX_TOOLS),
        available=_is_available(entry["env"]),
    )


def get_model(model_id: str) -> ModelSpec | None:
    """Look up a model by id; return None if unknown."""
    for entry in MODELS:
        if entry["id"] == model_id:
            return _build_spec(entry)
    return None


def list_models() -> list[ModelSpec]:
    """Return all models with availability computed from current env."""
    return [_build_spec(entry) for entry in MODELS]
