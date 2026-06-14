"""Phase 1.6 — FastAPI dependencies for the scope-gated API.

Two dependencies live here:

  * ``get_current_token`` — parses ``Authorization: Bearer <token>``,
    looks up the token in the :class:`TokenStore`, and returns the
    matching :class:`TokenRecord`. Returns 401 on missing / malformed
    / revoked / not-found tokens.
  * ``require_scope(*required)`` — factory that returns a FastAPI
    dependency enforcing the supplied scope set. Returns 403 if the
    token is valid but lacks the required scope. Uses the
    ANY-match semantics from :func:`harness.server.auth.scopes.has_scope`.

The ``auth_required`` escape hatch
----------------------------------
If ``app.state.auth_required`` is False (set by the lifespan handler
from ``settings.auth_required``), both dependencies short-circuit
and behave as if the request had a fully-scoped token. This is the
'open dev mode' the test suite and local development rely on. In
production, ``auth_required=True`` is the default and routes are
enforced normally.

Error model
-----------
We return JSON ``{"detail": "..."}`` to match FastAPI's default
``HTTPException`` shape. The detail messages are deliberately
informative for 403 ('missing required scope: X (have: A, B)') so
the CLI can debug scope mismatches without consulting docs, but
deliberately vague for 401 ('invalid or revoked token' covers
both 'not found' and 'revoked' so an attacker can't enumerate
token hashes by status code).
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, Header, HTTPException, Request, status

from harness.server.auth.scopes import Scope, has_scope
from harness.server.auth.tokens import TokenRecord, TokenStore


async def get_token_store(request: Request) -> TokenStore:
    """Pull the :class:`TokenStore` from ``app.state`` or 503.

    The store is set up in the FastAPI lifespan handler. If it's
    missing, we surface a 503 — this should only happen if lifespan
    init failed (a programmer error, not a runtime condition).
    """
    store = getattr(request.app.state, "token_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TokenStore not initialised (server lifespan init failed)",
        )
    return store


def _is_auth_required(request: Request) -> bool:
    """Read the auth-required flag from app.state.

    Defaults to True when the flag is missing — the safe default
    is to require auth, so a misconfigured lifespan that forgets
    to set the flag is enforced rather than silently open.
    """
    return bool(getattr(request.app.state, "auth_required", True))


async def get_current_token(
    request: Request,
    store: TokenStore = Depends(get_token_store),
    authorization: str | None = Header(default=None),
) -> TokenRecord | None:
    """Parse the Bearer token, look it up, return the record (or None in open mode).

    Returns:
      * ``TokenRecord`` when auth is required AND the token is valid
      * ``None`` when ``auth_required=False`` (open dev mode) — the
        route is then responsible for treating the request as
        authoritative (or for using ``require_scope`` which will
        also short-circuit)
      * Raises ``HTTPException(401)`` when auth is required but the
        token is missing / malformed / not-found / revoked

    The ``None`` return in open mode is intentional — it lets the
    route know auth was bypassed so it can log that fact if it
    wants to (e.g. for an audit trail in dev). The ``require_scope``
    dependency handles the same case differently (raises nothing,
    passes through) because by the time scope is checked we want
    the route to think the user is fully privileged.
    """
    if not _is_auth_required(request):
        return None  # open dev mode — route treats as authoritative

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Parse "Bearer X" — case-insensitive scheme, single space,
    # exactly one token (no commas). Anything else -> 401.
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Authorization header (expected 'Bearer <token>')",
            headers={"WWW-Authenticate": "Bearer"},
        )
    plaintext = parts[1].strip()

    record = await store.lookup(plaintext)
    if record is None:
        # We deliberately do NOT distinguish 'not found' from
        # 'revoked' in the error message — an attacker probing
        # for valid token hashes should not get a different
        # response for a known-revoked token vs an unknown one.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return record


def require_scope(*required: Scope) -> Callable:
    """Factory: return a FastAPI dependency that enforces the given scopes.

    The returned dependency depends on ``get_current_token`` and
    applies the ANY-match check from :func:`has_scope`. In open
    dev mode (``auth_required=False``) the check is skipped and
    the dependency passes through silently.

    Usage::

        @router.get("/jobs")
        async def list_jobs(
            token: TokenRecord | None = Depends(require_scope(Scope.AGENTS_READ)),
        ):
            ...

    The dependency is created once at import time (when the route
    is defined) and shared across all requests — so we capture
    ``required`` in a closure rather than reaching for a class.
    """
    required_set = frozenset(required)

    async def _dep(
        request: Request,
        token: TokenRecord | None = Depends(get_current_token),
    ) -> TokenRecord | None:
        # Open dev mode — skip scope check, return whatever the
        # token dep returned (None in open mode, or a record when
        # auth_required=True but the route itself doesn't need a
        # specific scope, which would be unusual but valid).
        if not _is_auth_required(request):
            return token

        if token is None:
            # ``get_current_token`` would have raised 401 if
            # auth was required and the header was missing, so
            # this branch is only reachable when a parent
            # dependency overrode the auth check (rare). We
            # still want to fail closed.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not has_scope(token.scopes, required_set):
            have = ", ".join(sorted(s.value for s in token.scopes)) or "(none)"
            want = ", ".join(sorted(s.value for s in required_set))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"missing required scope: {want} (have: {have})"
                ),
            )
        return token

    return _dep


__all__ = ["get_current_token", "get_token_store", "require_scope"]
