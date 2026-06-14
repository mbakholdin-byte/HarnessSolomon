"""GET /api/models — list of available LLM models with availability flags.

The endpoint is intentionally cheap (no LLM calls, no DB) so the frontend
can poll it freely while building the model-selector dropdown.
"""
from __future__ import annotations

from fastapi import APIRouter

from harness.server.llm.models import list_models

router = APIRouter()


@router.get("/models")
async def get_models() -> list[dict]:
    """Return all catalog models with availability computed from env.

    Response shape (per model):
        {
          "id": "MiniMax-M2.7",
          "provider": "minimax",
          "tier": "T3",
          "context": 200000,
          "available": true|false,
          "pricing_input": 0.30,
          "pricing_output": 0.60,
        }
    """
    return [
        {
            "id": spec.id,
            "provider": spec.provider,
            "tier": spec.tier,
            "context": spec.ctx,
            "available": spec.available,
            "pricing_input": spec.pricing_input,
            "pricing_output": spec.pricing_output,
        }
        for spec in list_models()
    ]
