"""Phase 7.4 WI-01 v1.32.0 — Marketplace REST API.

Provides public read-only endpoints for browsing the local plugin
marketplace catalogue.

Endpoints:
  * ``GET /api/v1/marketplace/plugins``      — list plugins
  * ``GET /api/v1/marketplace/plugins/{name}`` — plugin detail

RBAC
----
All endpoints require ``Scope.PLUGINS_READ``.  In open dev mode
(``settings.auth_required=False``) the scope check is bypassed.

Trust boundary (CRITICAL):
    This module imports from ``harness.plugins.manifest_v2``,
    ``harness.server.auth.deps``, and ``harness.server.auth.scopes``.
    It does NOT import ``harness.agents`` — the trust boundary is
    preserved at the AST level.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from harness.plugins.marketplace import MarketplaceManager
from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/marketplace", tags=["marketplace"])

_marketplace_read = require_scope(Scope.PLUGINS_READ)


# ── Pydantic response models ─────────────────────────────────────


class MarketplacePluginDetail(BaseModel):
    """Public-facing plugin manifest exposed via the marketplace API."""

    model_config = {"extra": "forbid"}

    name: str
    version: str
    author: str
    description: str
    min_harness_version: str
    permissions: list[str]
    signature: str | None
    public_key: str | None
    entry_point: str
    homepage: str | None
    repository: str | None
    keywords: list[str]


class MarketplaceListResponse(BaseModel):
    """``GET /api/v1/marketplace/plugins`` response."""

    plugins: list[MarketplacePluginDetail]
    total: int


# ── Dependency: resolve marketplace ──────────────────────────────


def _get_marketplace(request: Request) -> MarketplaceManager:
    """Pull the ``MarketplaceManager`` from ``app.state.marketplace``.

    Set up by the FastAPI lifespan handler.  If missing, return 503
    so the caller knows the marketplace is not initialised.
    """
    marketplace = getattr(request.app.state, "marketplace", None)
    if marketplace is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Marketplace not initialised (server lifespan init failed)",
        )
    return marketplace


def _manifest_to_detail(m: Any) -> MarketplacePluginDetail:
    """Convert a :class:`PluginManifestV2` into a Pydantic response model."""
    return MarketplacePluginDetail(
        name=m.name,
        version=m.version,
        author=m.author,
        description=m.description,
        min_harness_version=m.min_harness_version,
        permissions=list(m.permissions),
        signature=m.signature,
        public_key=m.public_key,
        entry_point=m.entry_point,
        homepage=m.homepage,
        repository=m.repository,
        keywords=list(m.keywords),
    )


# ── Routes ───────────────────────────────────────────────────────


@router.get("/plugins", response_model=MarketplaceListResponse)
async def list_marketplace_plugins(
    request: Request,
    keyword: str | None = Query(default=None, description="Filter by keyword"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    _token: Any = Depends(_marketplace_read),
) -> MarketplaceListResponse:
    """List available plugins in the marketplace.

    Supports optional ``keyword`` filtering (case-insensitive
    substring match against name, description, and keywords)
    and ``limit`` / ``offset`` pagination.
    """
    marketplace = _get_marketplace(request)
    # Get total matching (before pagination) by doing a full search.
    all_matching = marketplace.list_plugins(keyword=keyword, limit=10_000, offset=0)
    total = len(all_matching)
    page = all_matching[offset : offset + limit]
    return MarketplaceListResponse(
        plugins=[_manifest_to_detail(m) for m in page],
        total=total,
    )


@router.get("/plugins/{name}", response_model=MarketplacePluginDetail)
async def get_marketplace_plugin(
    name: str,
    request: Request,
    _token: Any = Depends(_marketplace_read),
) -> MarketplacePluginDetail:
    """Get a single plugin manifest by name.

    Returns 404 if the plugin is not found in the marketplace.
    """
    marketplace = _get_marketplace(request)
    manifest = marketplace.get(name)
    if manifest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"plugin {name!r} not found in marketplace",
        )
    return _manifest_to_detail(manifest)


__all__ = ["router"]
