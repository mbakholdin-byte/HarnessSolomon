"""v1.31.0: Plugins admin REST endpoints.

Operator-facing surface for listing, inspecting, enabling, and
disabling plugins at runtime. Provides:

  * ``GET /api/v1/plugins``          — list loaded plugins
  * ``GET /api/v1/plugins/{name}``   — single plugin details
  * ``POST /api/v1/plugins/{name}/enable``  — re-enable a disabled plugin
  * ``POST /api/v1/plugins/{name}/disable`` — disable (unload) a plugin

RBAC
----

All endpoints require ``Scope.PLUGINS_ADMIN``. In open dev mode
(``settings.auth_required=False``) the scope check is bypassed.

Trust boundary
--------------

This module imports only from stdlib, FastAPI, harness.config,
harness.plugins, and harness.server.auth. It does NOT import
from ``harness.agents`` — the trust boundary is preserved at
the AST level.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from harness.plugins import PluginRegistry
from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plugins-admin"])

_plugins_admin = require_scope(Scope.PLUGINS_ADMIN)


# === Pydantic models ===

class PluginDetail(BaseModel):
    """Public-facing plugin info exposed via the API.

    Mirrors :class:`~harness.plugins.PluginInfo` with the addition
    of ``enabled`` (derived from the registry's disabled set) and
    without internal fields.
    """

    model_config = {"extra": "forbid"}

    name: str
    version: str
    source_path: str
    enabled: bool
    hooks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)


class PluginListResponse(BaseModel):
    """``GET /api/v1/plugins`` response."""

    plugins: list[PluginDetail]
    total: int


class PluginStatusResponse(BaseModel):
    """``POST /api/v1/plugins/{name}/enable`` or ``/disable`` response."""

    name: str
    enabled: bool


# === Dependency: resolve registry ===

def _get_registry(request: Request) -> PluginRegistry:
    """Pull the ``PluginRegistry`` from ``app.state.plugin_registry``.

    Set up by the FastAPI lifespan handler. If missing, return 503
    so the operator knows the plugin system is not initialised.
    """
    registry = getattr(request.app.state, "plugin_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="plugin_registry not initialised (plugins disabled)",
        )
    return registry


# === Routes ===

@router.get("/plugins", response_model=PluginListResponse)
async def list_plugins(
    request: Request,
    _token: Any = Depends(_plugins_admin),
) -> PluginListResponse:
    """List all loaded plugins with their enabled/disabled state."""
    registry = _get_registry(request)
    infos = registry.list_plugins()
    # Also include disabled plugins (name-only, no hooks/tools).
    result: list[PluginDetail] = []
    seen: set[str] = set()
    for info in infos:
        seen.add(info.name)
        result.append(PluginDetail(
            name=info.name,
            version=info.version,
            source_path=info.source_path,
            enabled=not registry.is_disabled(info.name),
            hooks=list(info.hooks),
            tools=list(info.tools),
            scopes=list(info.scopes),
        ))
    # Append disabled-but-known plugins as empty stubs.
    for name in sorted(registry._disabled):
        if name not in seen:
            result.append(PluginDetail(
                name=name,
                version="—",
                source_path="—",
                enabled=False,
                hooks=[],
                tools=[],
                scopes=[],
            ))
    result.sort(key=lambda p: p.name)
    return PluginListResponse(plugins=result, total=len(result))


@router.get("/plugins/{name}", response_model=PluginDetail)
async def get_plugin(
    name: str,
    request: Request,
    _token: Any = Depends(_plugins_admin),
) -> PluginDetail:
    """Get a single plugin by name.

    Returns 404 if the plugin is not loaded and not in the
    disabled set.
    """
    registry = _get_registry(request)
    info = registry.get_plugin(name)
    if info is not None:
        return PluginDetail(
            name=info.name,
            version=info.version,
            source_path=info.source_path,
            enabled=not registry.is_disabled(name),
            hooks=list(info.hooks),
            tools=list(info.tools),
            scopes=list(info.scopes),
        )
    # Check if it's a known disabled plugin.
    if registry.is_disabled(name):
        return PluginDetail(
            name=name,
            version="—",
            source_path="—",
            enabled=False,
            hooks=[],
            tools=[],
            scopes=[],
        )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"plugin {name!r} not found",
    )


@router.post("/plugins/{name}/enable", response_model=PluginStatusResponse)
async def enable_plugin(
    name: str,
    request: Request,
    _token: Any = Depends(_plugins_admin),
) -> PluginStatusResponse:
    """Re-enable a previously disabled plugin.

    Removes the administrative block. The plugin must be reloaded
    via the loader for its hooks/tools to become active again —
    this endpoint only clears the disabled flag.

    Returns 200 if the plugin was disabled and is now re-enabled.
    Returns 200 with ``enabled=True`` if it was already active
    (idempotent). Returns 404 if the plugin name is completely
    unknown.
    """
    registry = _get_registry(request)
    # Check existence: either loaded or known-disabled.
    if registry.get_plugin(name) is None and not registry.is_disabled(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"plugin {name!r} not found",
        )
    was_disabled = registry.is_disabled(name)
    registry.enable(name)
    logger.info(
        "plugins_admin: enable name=%s was_disabled=%s", name, was_disabled,
    )
    return PluginStatusResponse(name=name, enabled=True)


@router.post("/plugins/{name}/disable", response_model=PluginStatusResponse)
async def disable_plugin(
    name: str,
    request: Request,
    _token: Any = Depends(_plugins_admin),
) -> PluginStatusResponse:
    """Disable a plugin: unload + mark disabled.

    Removes the plugin's hooks and tools from the registry and
    marks it as disabled so it cannot be re-loaded until
    ``enable`` is called.

    Returns 200 if the plugin was loaded and is now disabled.
    Returns 404 if the plugin is not currently loaded.
    """
    registry = _get_registry(request)
    found = registry.disable(name)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"plugin {name!r} not loaded",
        )
    logger.info("plugins_admin: disable name=%s", name)
    return PluginStatusResponse(name=name, enabled=False)


__all__ = ["router"]
