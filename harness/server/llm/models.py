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

MODELS: list[dict] = [
    {
        "id": "MiniMax-M2.7",
        "provider": "minimax",
        "tier": "T3",
        "env": "MINIMAX_API_KEY",
        "ctx": 200000,
        "pricing_input": 0.30,   # $ per 1M tokens
        "pricing_output": 0.60,
    },
    {
        "id": "glm-4.7",
        "provider": "zhipuai",
        "tier": "T3",
        "env": "ZHIPUAI_API_KEY",
        "ctx": 128000,
        "pricing_input": 0.10,
        "pricing_output": 0.10,
    },
    {
        "id": "moonshot-v1-128k",
        "provider": "moonshot",
        "tier": "T3",
        "env": "MOONSHOT_API_KEY",
        "ctx": 128000,
        "pricing_input": 0.20,
        "pricing_output": 0.20,
    },
]


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
    available: bool = False  # computed from env at construction time


# === Helpers ===

def _is_available(env_var: str) -> bool:
    """True iff env_var is set to a non-empty string."""
    return bool(os.environ.get(env_var, "").strip())


def get_model(model_id: str) -> ModelSpec | None:
    """Look up a model by id; return None if unknown."""
    for entry in MODELS:
        if entry["id"] == model_id:
            return ModelSpec(
                id=entry["id"],
                provider=entry["provider"],
                tier=entry["tier"],
                env=entry["env"],
                ctx=entry["ctx"],
                pricing_input=entry["pricing_input"],
                pricing_output=entry["pricing_output"],
                available=_is_available(entry["env"]),
            )
    return None


def list_models() -> list[ModelSpec]:
    """Return all models with availability computed from current env."""
    return [
        ModelSpec(
            id=entry["id"],
            provider=entry["provider"],
            tier=entry["tier"],
            env=entry["env"],
            ctx=entry["ctx"],
            pricing_input=entry["pricing_input"],
            pricing_output=entry["pricing_output"],
            available=_is_available(entry["env"]),
        )
        for entry in MODELS
    ]
