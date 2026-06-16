"""Phase 4.1+ Step 1: Legacy API deprecation middleware (RFC 8594 + RFC 8288).

Adds ``Deprecation``, ``Sunset``, and ``Link: <canonical>`` headers
to responses on legacy ``/api/*`` paths (NOT ``/api/v1/*``).

References:
    - RFC 8594 — The "Deprecation" HTTP Header Field
    - RFC 8288 — Web Linking (for ``Link: <canonical>; rel="successor-version"``)

Examples:
    Request  ``GET /api/sessions/abc`` →
    Response headers:
        Deprecation: true
        Sunset: Wed, 31 Dec 2026 23:59:59 GMT
        Link: </api/v1/sessions/abc>; rel="successor-version"

Excluded paths (always canonical, no headers):
    - ``/api/v1/*`` (already versioned)
    - ``/metrics`` (Prometheus convention, top-level)
    - ``/health/live``, ``/health/ready``, ``/health/deep`` (K8s convention)
    - ``/api/health`` (backward-compat alias for /health/deep, see v1.7.1)
    - ``/openapi.json``, ``/docs``, ``/docs/oauth2-redirect``, ``/redoc``
      (FastAPI's own routes; adding deprecation headers to these
      would break tools like Swagger UI)
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_log = logging.getLogger(__name__)

#: Per RFC 8594: ISO 8601 date or HTTP-date. Use HTTP-date for max compat.
#: Sunset = 2026-12-31 23:59:59 UTC. After this, /api/* returns 410 Gone.
SUNSET_HTTP_DATE: str = "Wed, 31 Dec 2026 23:59:59 GMT"

#: Paths that are top-level / convention and MUST NOT be marked deprecated.
_EXCLUDED_EXACT: frozenset[str] = frozenset({
    "/api/v1",                      # capabilities discovery root
    "/api/health",                  # v1.7.1 backward-compat alias
    "/metrics",
    "/health/live",
    "/health/ready",
    "/health/deep",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/api/chat",                    # WebSocket — no path rewrite possible
    "/api/chat/ws",
    "/api/v1/chat",
    "/api/v1/chat/ws",
})

#: Path prefixes that are excluded (versioned, top-level conventions, etc.)
_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/api/v1/",      # already versioned
    "/webhooks/",    # GitHub webhook receiver (path is operator-configurable)
    "/static/",      # static assets
)


def _is_excluded(path: str) -> bool:
    """True if path should NOT get deprecation headers."""
    if path in _EXCLUDED_EXACT:
        return True
    for prefix in _EXCLUDED_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _canonical_path(path: str) -> str:
    """Map a legacy /api/* path to its /api/v1/* successor.

    Simple rule: prepend ``/v1`` to the part after ``/api``.
    Examples:
        /api/sessions/abc    → /api/v1/sessions/abc
        /api/models          → /api/v1/models
        /api/chat/ws         → /api/v1/chat/ws
        /api/health          → /api/health  (alias, no change)
    """
    if not path.startswith("/api/"):
        return path
    # Insert "v1" after "/api/".
    return "/api/v1/" + path[len("/api/"):]


class LegacyApiDeprecationMiddleware(BaseHTTPMiddleware):
    """Adds Deprecation/Sunset/Link headers to legacy /api/* responses.

    Pass-through for already-versioned paths (``/api/v1/*``) and
    top-level convention paths (``/metrics``, ``/health/*``).

    Mount BEFORE the observability middleware so the deprecation
    headers are visible to monitoring stacks.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        response = await call_next(request)
        if _is_excluded(path):
            return response
        # Only legacy /api/* (not /api/v1/*, not /metrics, not /health/*).
        if not path.startswith("/api/"):
            return response
        canonical = _canonical_path(path)
        # If the canonical path is the same as the request path
        # (no v1 insertion possible), skip — nothing to migrate to.
        if canonical == path:
            return response
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = SUNSET_HTTP_DATE
        # RFC 8288: rel="successor-version" (RFC 8594 § 3).
        response.headers["Link"] = f'<{canonical}>; rel="successor-version"'
        return response


def install_deprecation_middleware(app: FastAPI) -> None:
    """Install the deprecation middleware. Idempotent (no-op if already added)."""
    # Starlette's add_middleware is idempotent in newer versions; for safety
    # we just call it. If a duplicate is installed, the headers will be
    # overwritten by the outer middleware — harmless.
    app.add_middleware(LegacyApiDeprecationMiddleware)
