"""Phase 4.12 v1.22.0: Legacy ``/api/*`` → 410 Gone middleware (RFC 7231 + 8594).

When the operator enables ``legacy_apis_gone_enabled`` (default False,
opt-in), every request to a legacy ``/api/*`` path that is NOT already
versioned (``/api/v1/*``) is short-circuited with an
``HTTP 410 Gone`` response. The response carries:

  * ``Deprecation: true``           (RFC 8594)
  * ``Sunset: Wed, 31 Dec 2026 …``  (RFC 8594 § 3 — deprecation date)
  * ``Link: </api/v1/>; rel="successor-version"`` (RFC 8288)
  * a JSON body pointing clients at the migration guide.

The setting is **opt-in** so existing deployments continue to serve
legacy endpoints (with the existing ``LegacyApiDeprecationMiddleware``
headers) until the operator flips the switch.

Trust boundary: this module imports ONLY stdlib + FastAPI/Starlette.
It MUST NOT import from ``harness.agents`` (verified by the AST test
in ``tests/test_legacy_gone_v122.py``).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_log = logging.getLogger(__name__)

#: Per RFC 8594 § 3: HTTP-date after which the legacy surface is gone.
#: Mirrors the value used by :mod:`harness.server.deprecation`.
SUNSET_HTTP_DATE: str = "Wed, 31 Dec 2026 23:59:59 GMT"

#: Canonical migration URL surfaced in the JSON body + Link header.
#: Kept as a module constant so tests and operators can override it
#: at import time (e.g. for self-hosted docs).
MIGRATION_URL: str = "https://docs.harness/api/v1-migration"

#: Static 410 response body. Built once at import time.
_GONE_BODY: dict[str, str] = {
    "error": "Gone",
    "message": "Legacy endpoint moved to /api/v1/. See migration guide.",
    "migration_url": MIGRATION_URL,
}

#: Static 410 response headers. Built once at import time.
_GONE_HEADERS: dict[str, str] = {
    "Deprecation": "true",
    "Sunset": SUNSET_HTTP_DATE,
    "Link": '</api/v1/>; rel="successor-version"',
    "Content-Type": "application/json",
}


def _is_legacy_api_path(path: str) -> bool:
    """Return True iff ``path`` is a legacy ``/api/*`` route.

    Legacy means: starts with ``/api/`` but NOT ``/api/v1/``.
    Examples:
        /api/sessions/S1   → True   (legacy)
        /api/v1/sessions   → False  (already versioned)
        /metrics           → False  (not /api at all)
        /api/v1            → False  (exact /api/v1, no trailing slash)
    """
    if not path.startswith("/api/"):
        return False
    # ``/api/v1`` and ``/api/v1/...`` are the canonical surface.
    if path.startswith("/api/v1"):
        return False
    return True


class LegacyApisGoneMiddleware(BaseHTTPMiddleware):
    """Returns 410 Gone for legacy ``/api/*`` requests when enabled.

    The middleware reads the master switch from ``app.state`` (set
    by :func:`install_legacy_gone_middleware` from
    ``settings.legacy_apis_gone_enabled``). When the switch is False
    the middleware is a pure pass-through — the existing
    :class:`harness.server.deprecation.LegacyApiDeprecationMiddleware`
    continues to add deprecation headers without short-circuiting the
    response.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Cheap fast-path: non-legacy paths go straight through.
        path = request.url.path
        if not _is_legacy_api_path(path):
            return await call_next(request)

        # Read the opt-in flag from app.state (set at install time).
        # Default to False if the attribute is missing (defensive —
        # the installer always sets it, but middleware may be imported
        # standalone in tests).
        enabled = getattr(request.app.state, "legacy_apis_gone_enabled", False)
        if not enabled:
            return await call_next(request)

        # Short-circuit: 410 Gone with deprecation/sunset/link headers.
        _log.debug(
            "legacy_apis_gone: 410 for %s %s (sunset=%s)",
            request.method, path, SUNSET_HTTP_DATE,
        )
        return JSONResponse(
            status_code=410,
            headers=dict(_GONE_HEADERS),
            content=dict(_GONE_BODY),
        )


def install_legacy_gone_middleware(app: FastAPI, *, enabled: bool) -> None:
    """Install the legacy-apis-gone middleware.

    Args:
        app: the FastAPI app to install on.
        enabled: mirrors ``settings.legacy_apis_gone_enabled``. Stashed
            on ``app.state.legacy_apis_gone_enabled`` so the middleware
            can read it per-request without re-resolving settings.

    Idempotent: calling twice on the same app is a no-op for the
    state flag (last write wins) and adds the middleware at most once
    per call (Starlette deduplicates by class).
    """
    app.state.legacy_apis_gone_enabled = bool(enabled)
    app.add_middleware(LegacyApisGoneMiddleware)
