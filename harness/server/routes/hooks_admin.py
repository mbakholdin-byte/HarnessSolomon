"""v1.31.0: Hooks admin REST endpoints.

Operator-facing surface for listing, inspecting, enabling, and
disabling hooks at runtime. Provides:

  * ``GET /api/v1/hooks``          — list all hooks (builtin + custom)
  * ``GET /api/v1/hooks/{id}``     — single hook details
  * ``POST /api/v1/hooks/{id}/enable``  — flip on
  * ``POST /api/v1/hooks/{id}/disable`` — flip off

RBAC
----

All endpoints require ``Scope.HOOKS_ADMIN``. In open dev mode
(``settings.auth_required=False``) the scope check is bypassed.

Trust boundary
--------------

This module imports only from stdlib, FastAPI, harness.config,
harness.hooks (registry + events), and harness.server.auth. It
does NOT import from ``harness.agents`` — the trust boundary
is preserved at the AST level.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec
from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["hooks-admin"])

_hooks_admin = require_scope(Scope.HOOKS_ADMIN)


# === Pydantic models ===

class HookDetail(BaseModel):
    """Public-facing hook info exposed via the API.

    Deliberately excludes the ``callable`` reference (not JSON-
    serialisable) and internal fields like ``script_path`` / ``url``
    / ``headers`` / ``model`` / ``prompt`` (those are visible in
    ``transport_specific``).
    """

    model_config = {"extra": "forbid"}

    hook_id: str
    event: str  # EventType name as string
    transport: str
    enabled: bool
    timeout_ms: int | None = None
    priority: int = 100
    matcher: str = ""

    @classmethod
    def from_spec(cls, spec: HookSpec) -> "HookDetail":
        return cls(
            hook_id=spec.hook_id,
            event=spec.event.value,
            transport=spec.transport,
            enabled=spec.enabled,
            timeout_ms=spec.timeout_ms,
            priority=spec.priority,
            matcher=spec.matcher,
        )


class HookListResponse(BaseModel):
    """``GET /api/v1/hooks`` response."""

    hooks: list[HookDetail]
    total: int


class HookStatusResponse(BaseModel):
    """``POST /api/v1/hooks/{id}/enable`` or ``/disable`` response."""

    hook_id: str
    enabled: bool


# === Dependency: resolve registry ===

def _get_registry(request: Request) -> HookRegistry:
    """Pull the ``HookRegistry`` from ``app.state.hook_runner``.

    Set up by the FastAPI lifespan handler. If missing, return 503
    so the operator knows the hooks framework is not initialised.
    """
    runner = getattr(request.app.state, "hook_runner", None)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="hook_runner not initialised (hooks framework disabled)",
        )
    return runner._registry


# === Routes ===

@router.get("/hooks", response_model=HookListResponse)
async def list_hooks(
    request: Request,
    _token: Any = Depends(_hooks_admin),
) -> HookListResponse:
    """List all registered hooks (builtin + custom) with on/off state.

    Returns hooks sorted by event, then by priority.
    """
    registry = _get_registry(request)
    specs = registry.all_specs()
    # Sort: event name, then priority, then hook_id for determinism.
    specs.sort(key=lambda s: (s.event.value, s.priority, s.hook_id))
    hooks = [HookDetail.from_spec(s) for s in specs]
    return HookListResponse(hooks=hooks, total=len(hooks))


@router.get("/hooks/{hook_id}", response_model=HookDetail)
async def get_hook(
    hook_id: str,
    request: Request,
    _token: Any = Depends(_hooks_admin),
) -> HookDetail:
    """Get a single hook by id.

    Returns 404 if the hook does not exist.
    """
    registry = _get_registry(request)
    spec = registry.get_spec(hook_id)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"hook {hook_id!r} not found",
        )
    return HookDetail.from_spec(spec)


@router.post("/hooks/{hook_id}/enable", response_model=HookStatusResponse)
async def enable_hook(
    hook_id: str,
    request: Request,
    _token: Any = Depends(_hooks_admin),
) -> HookStatusResponse:
    """Enable a hook.

    Returns 404 if the hook does not exist. Idempotent — calling
    enable on an already-enabled hook succeeds (enabled=True).
    """
    registry = _get_registry(request)
    found = await registry.enable(hook_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"hook {hook_id!r} not found",
        )
    logger.info("hooks_admin: enable hook_id=%s", hook_id)
    return HookStatusResponse(hook_id=hook_id, enabled=True)


@router.post("/hooks/{hook_id}/disable", response_model=HookStatusResponse)
async def disable_hook(
    hook_id: str,
    request: Request,
    _token: Any = Depends(_hooks_admin),
) -> HookStatusResponse:
    """Disable a hook.

    Returns 404 if the hook does not exist. Idempotent — calling
    disable on an already-disabled hook succeeds (enabled=False).
    """
    registry = _get_registry(request)
    found = await registry.disable(hook_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"hook {hook_id!r} not found",
        )
    logger.info("hooks_admin: disable hook_id=%s", hook_id)
    return HookStatusResponse(hook_id=hook_id, enabled=False)


__all__ = ["router"]
