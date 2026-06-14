"""Phase 1.6 — ``/api/v1/memory/*`` routes.

Endpoints:
  - ``GET  /api/v1/memory/search?q=X&k=5`` — search the 4-layer
    memory (Phase 1.6: requires ``memory.read``)
  - ``POST /api/v1/memory/notes`` — write a new memory note
    (Phase 1.6: requires ``memory.write``)
  - ``GET  /api/v1/memory/stats`` — per-layer counts
    (Phase 1.6: requires ``memory.read``)

The actual memory work lives in
:mod:`harness.server.agent.memory_v1` (the bridge). This module
is the FastAPI adapter — request parsing, scope checks, and
response shaping.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from harness.server.agent import memory_v1
from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

router = APIRouter()


# Reusable dependency handles. ``_mem_read`` / ``_mem_write`` are
# created at import time and shared across the three routes below.
_mem_read = require_scope(Scope.MEMORY_READ)
_mem_write = require_scope(Scope.MEMORY_WRITE)


# === Pydantic models ===

class _MemoryHit(BaseModel):
    """One search result."""

    id: str
    layer: str
    source: str
    text: str
    tags: list[str]
    agent_id: str | None = None


class _SearchResponse(BaseModel):
    """``GET /api/v1/memory/search`` response."""

    query: str
    k: int
    hits: list[_MemoryHit]


class _WriteNoteRequest(BaseModel):
    """``POST /api/v1/memory/notes`` body."""

    text: str = Field(..., min_length=1, max_length=8000)
    layer: str = Field(
        default="L2",
        description="One of L1/L2/L2.5/L3/L4. Default L2 (semantic).",
    )
    tags: list[str] = Field(default_factory=list)


class _WriteNoteResponse(BaseModel):
    """``POST /api/v1/memory/notes`` response."""

    id: str
    layer: str
    source: str
    tags: list[str]
    agent_id: str | None = None


class _StatsResponse(BaseModel):
    """``GET /api/v1/memory/stats`` response."""

    agent_id: str
    layers: dict[str, Any]


# === Routes ===

@router.get("/search", response_model=_SearchResponse)
async def search_memory(
    q: str,
    k: int = 5,
    _token: Any = Depends(_mem_read),
) -> _SearchResponse:
    """Search the 4-layer memory with BM25 + identity rerank.

    Query parameters:
      * ``q`` — required, the search string
      * ``k`` — optional, default 5, max 50

    Returns 422 on missing ``q`` (handled by FastAPI's required-
    query-param validation) and 200 with an empty ``hits`` list
    on no matches.
    """
    if k < 1 or k > 50:
        raise HTTPException(
            status_code=422, detail="k must be between 1 and 50",
        )
    if not q.strip():
        raise HTTPException(
            status_code=422, detail="q must be a non-empty string",
        )
    raw_hits = memory_v1.search(q.strip(), k=k)
    hits = [_MemoryHit(**h) for h in raw_hits]
    return _SearchResponse(query=q.strip(), k=k, hits=hits)


@router.post("/notes", response_model=_WriteNoteResponse, status_code=201)
async def write_note(
    body: _WriteNoteRequest,
    _token: Any = Depends(_mem_write),
) -> _WriteNoteResponse:
    """Dual-write a new memory note to the unified facade.

    Pydantic enforces non-empty ``text`` and a sane ``layer``.
    The bridge does the actual write; this route just shapes
    the response.
    """
    if body.layer not in ("L1", "L2", "L2.5", "L3", "L4"):
        raise HTTPException(
            status_code=422,
            detail=f"unknown layer: {body.layer!r} "
            f"(valid: L1, L2, L2.5, L3, L4)",
        )
    try:
        result = memory_v1.write_note(
            text=body.text, layer=body.layer, tags=body.tags,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _WriteNoteResponse(**result)


@router.get("/stats", response_model=_StatsResponse)
async def memory_stats(
    _token: Any = Depends(_mem_read),
) -> _StatsResponse:
    """Return per-layer entry counts (cheap, best-effort).

    The ``L1`` count is read from the on-disk JSONL file
    (one line per entry). Other layers report ``available: true``
    — a precise per-entry count would require a per-adapter
    ``count()`` method (Phase 3 follow-up).
    """
    return _StatsResponse(**memory_v1.stats())


__all__ = ["router"]
