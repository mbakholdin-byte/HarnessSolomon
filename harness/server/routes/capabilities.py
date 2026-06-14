"""Phase 1.6 — ``GET /api/v1/capabilities`` endpoint.

This is the always-public self-description endpoint clients hit
to learn what scopes they need before authenticating. It is the
ONLY route in the ``/api/v1/`` namespace that is exempt from the
scope check — by design, so a client with no token can still
discover the server's capability surface.

Response shape::

    {
      "server_version": "0.6.0",
      "auth_required": true,
      "scopes_available": [
        {"name": "agents.read", "description": "..."},
        ...
      ],
      "endpoints": [
        {"method": "GET", "path": "/api/v1/agents/jobs", "scopes": ["agents.read"]},
        ...
      ]
    }

The route is registered at ``/api/v1/capabilities`` (no
prefix in :func:`harness.server.app.create_app`).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from harness.config import settings
from harness.server.auth.route_registry import collect_endpoints
from harness.server.auth.scopes import ALL_SCOPES, scope_description

router = APIRouter()


class _ScopeDescriptor(BaseModel):
    """One row in the ``scopes_available`` array."""

    name: str
    description: str


class _CapabilitiesResponse(BaseModel):
    """JSON shape of the capabilities response."""

    server_version: str
    auth_required: bool
    scopes_available: list[_ScopeDescriptor]
    endpoints: list[dict[str, Any]]


@router.get("/capabilities", response_model=_CapabilitiesResponse)
async def get_capabilities(request: Request) -> _CapabilitiesResponse:
    """Return the server's self-description.

    Public on purpose — this is the discovery endpoint. The
    ``auth_required`` flag tells the client whether subsequent
    calls will need a token; the ``scopes_available`` array
    is the closed set the server recognises; ``endpoints``
    is built live from the app's mounted routes via
    :func:`harness.server.auth.route_registry.collect_endpoints`.
    """
    specs = collect_endpoints(request.app)
    return _CapabilitiesResponse(
        # The app's own ``version`` attribute is set by
        # FastAPI(title=..., version=...) in create_app().
        server_version=getattr(request.app, "version", "0.0.0"),
        auth_required=settings.auth_required,
        scopes_available=[
            _ScopeDescriptor(name=s.value, description=scope_description(s))
            for s in sorted(ALL_SCOPES, key=lambda x: x.value)
        ],
        endpoints=[s.to_dict() for s in specs],
    )


__all__ = ["router"]
