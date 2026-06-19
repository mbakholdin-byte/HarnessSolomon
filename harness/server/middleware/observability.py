"""Phase 4.1 Step 6.2: HTTP request metrics middleware.

FastAPI middleware that records ``http_requests_total{route,method,status}``
and ``http_request_duration_seconds{route,method}`` for every request.

Route label uses the FastAPI route template (e.g. ``/api/v1/agents/jobs/{id}``)
to avoid cardinality explosion — never the raw path. Falls back to a
shortened raw path if no route matched (404, 405).
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from harness.observability import emit_http_request

_log = logging.getLogger(__name__)

# Strip the trailing path parameter so the route template is used
# (e.g. /api/v1/agents/jobs/abc123 → /api/v1/agents/jobs/{id}).
# This is already what FastAPI's request.scope["route"].path gives us;
# we only fall back to path-shrinking when no route matched.

_MAX_FALLBACK_PATH_LEN = 64
_PATH_NUMERIC_RE = re.compile(r"/\d+(?=/|$)")
_PATH_UUID_RE = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)", re.IGNORECASE)


def _shrink_path(path: str) -> str:
    """Reduce cardinality of unmatched paths (404/405)."""
    if len(path) <= _MAX_FALLBACK_PATH_LEN:
        return path
    return _MAX_FALLBACK_PATH_LEN * "x" + "..."


def _normalize_fallback(path: str) -> str:
    """Replace obvious IDs in an unmatched path with placeholders."""
    p = _PATH_UUID_RE.sub("/{uuid}", path)
    p = _PATH_NUMERIC_RE.sub("/{id}", p)
    return p


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Records HTTP request log + metrics. Fail-open."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.monotonic()
        # Ensure each request has a request_id (idempotent — reuse if header set).
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 — middleware must not eat errors
            duration = time.monotonic() - start
            route = self._route_label(request, fallback="/_unmatched_")
            emit_http_request(
                method=request.method,
                route=route,
                status=500,
                duration_s=duration,
                request_id=request_id,
            )
            _log.debug("observability middleware caught: %s", exc)
            raise
        duration = time.monotonic() - start
        route = self._route_label(request, fallback="/_unmatched_")
        emit_http_request(
            method=request.method,
            route=route,
            status=response.status_code,
            duration_s=duration,
            request_id=request_id,
        )
        response.headers.setdefault("x-request-id", request_id)
        return response

    @staticmethod
    def _route_label(request: Request, *, fallback: str) -> str:
        route = request.scope.get("route")
        # FastAPI's APIRoute sets ``path`` to the template (with {id}, etc.).
        template = getattr(route, "path", None) if route is not None else None
        if template:
            return template
        path = request.url.path
        return _normalize_fallback(_shrink_path(path) or fallback)


def install_observability_middleware(app: FastAPI) -> None:
    """Install the observability middleware. Idempotent."""
    # Add the middleware BEFORE CORS so that even CORS-rejected
    # preflight failures are observable.
    app.add_middleware(ObservabilityMiddleware)
