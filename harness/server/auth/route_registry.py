"""Phase 1.6 — Introspect mounted FastAPI routes to build a capabilities manifest.

The :func:`collect_endpoints` function walks ``app.routes`` and
extracts the (method, path, required scopes) tuples for every
route that uses :func:`harness.server.auth.deps.require_scope`.
The capabilities endpoint (:mod:`harness.server.routes.capabilities`)
returns these tuples so clients can self-discover which scopes
they need to call which endpoints — no docs-rounding required.

Why introspection
-----------------
Hard-coding the route manifest in the capabilities endpoint
would drift the moment a route is added or renamed. Reading the
metadata from the live FastAPI app is the only single-source
of truth. The dep extraction works because we tag every scope
requirement with a marker class attribute (``Scope`` is a
``str``-mixin Enum, so we can pattern-match on it).

Routes without any ``require_scope`` dep are NOT included in
the capabilities response — they are either legacy ``/api/*``
(open) or always-public (``/api/v1/capabilities`` itself).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from fastapi import FastAPI
from fastapi.routing import APIRoute

from harness.server.auth.scopes import Scope


@dataclass(frozen=True)
class EndpointSpec:
    """Public-facing description of one (method, path) pair.

    ``scopes`` is the set of scopes the route requires via
    :func:`require_scope`. Empty set means the route is
    public (no scope check). Compound checks (e.g. ``POST
    /api/v1/agents/jobs`` requires ``agents.write`` AND —
    conditionally — ``agents.pr``) are represented as the
    union of all scopes mentioned; the conditional logic
    lives in the route itself, not in the capabilities
    manifest. Clients reading this should treat any scope in
    the set as a 'may be required' hint.
    """

    method: str
    path: str
    scopes: frozenset[Scope]

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "scopes": sorted(s.value for s in self.scopes),
        }


def _extract_scopes_from_deps(deps: Iterable[Any]) -> set[Scope]:
    """Walk a list of FastAPI dependencies, collect all ``Scope`` values.

    We use a marker attribute on the dep callable:
    ``dep._required_scopes`` (set by :func:`require_scope`).
    This is intentional — relying on a private attribute
    means we don't have to parse the function signature
    (which is fragile: FastAPI's dep tree is resolved and
    the original ``require_scope(...)`` call is not
    visible from the dep's defaults).
    """
    found: set[Scope] = set()
    for dep in deps:
        marker = getattr(dep, "_required_scopes", None)
        if marker is None:
            continue
        if not isinstance(marker, frozenset | set):
            continue
        for s in marker:
            if isinstance(s, Scope):
                found.add(s)
    return found


def collect_endpoints(app: FastAPI) -> list[EndpointSpec]:
    """Walk the app and return one :class:`EndpointSpec` per scoped route.

    Routes are deduplicated by (method, path) — a single
    FastAPI route can be reachable via multiple methods
    (e.g. a route that handles both GET and POST), and we
    emit one spec per method to keep the JSON shape stable.

    Routes outside ``/api/v1/`` are excluded — they are
    legacy (sessions, chat, models, health) and stay open
    in Phase 1.6. Including them in capabilities would
    imply they require auth, which would mislead clients.
    """
    out: list[EndpointSpec] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # Mounts, WebSocket routes — skip
        # Walk the entire dep tree. ``Dependant`` objects wrap
        # the underlying callable in ``.call``; for the route's
        # own endpoint, the callable is ``route.endpoint`` itself.
        all_deps: list[Any] = []
        for dep in route.dependant.dependencies:
            if hasattr(dep, "call") and dep.call is not None:
                all_deps.append(dep.call)
            # Also recurse into nested deps (the auth deps
            # chain through ``get_current_token`` and
            # ``get_token_store``).
            for nested in dep.dependencies:
                if hasattr(nested, "call") and nested.call is not None:
                    all_deps.append(nested.call)
        all_deps.append(route.endpoint)
        scopes = _extract_scopes_from_deps(all_deps)
        if not scopes:
            continue  # unscoped route → not in capabilities
        path = _normalise_path(route.path)
        if not path.startswith("/api/v1/"):
            continue
        for method in sorted(route.methods - {"HEAD"}):
            out.append(
                EndpointSpec(
                    method=method,
                    path=path,
                    scopes=frozenset(scopes),
                )
            )
    return out


def _normalise_path(raw: str) -> str:
    """FastAPI stores paths with the full prefix already; nothing to strip.

    But :class:`fastapi.routing.APIRoute` may use the FastAPI
    ``prefix`` field separately — we want the *resolved* path
    the client sees. FastAPI's ``route.path`` is already the
    resolved path (FastAPI composes the prefix at mount time),
    so this helper is a no-op for now but exists so future
    changes (e.g. mounting under a different prefix) can be
    adapted here without touching the caller.
    """
    return raw


__all__ = ["EndpointSpec", "collect_endpoints"]
