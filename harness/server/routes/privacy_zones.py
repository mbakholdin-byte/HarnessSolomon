"""Phase 5.3 v1.25.0 — ``/api/v1/privacy/zones`` CRUD REST API.

Runtime management of privacy zone rules via a REST surface. The
endpoints operate on an in-memory :class:`PrivacyZoneStore` (mounted on
``app.state``) that supplements the Settings-loaded rules from
``privacy_zone_patterns`` / ``privacy_zone_per_action``.

Endpoints
---------
  * ``GET    /api/v1/privacy/zones``       — list all zones (``privacy.read``)
  * ``GET    /api/v1/privacy/zones/{id}``  — get one zone (``privacy.read``)
  * ``POST   /api/v1/privacy/zones``       — create a zone (``privacy.write``)
  * ``PUT    /api/v1/privacy/zones/{id}``  — update a zone (``privacy.write``)
  * ``DELETE /api/v1/privacy/zones/{id}``  — delete a zone (``privacy.write``)

Trust boundary
--------------
This module imports ONLY from:
  * ``harness.config`` — settings
  * ``harness.privacy.zone_config`` — ``ZoneRule`` / ``ZoneAction``
  * ``harness.server.auth.deps`` / ``harness.server.auth.scopes``

It does NOT import ``harness.agents.*``. An AST test in
``tests/test_privacy_zones_api_v125.py`` enforces this.

Mount
-----
The router is mounted conditionally in ``app.py`` only when
``settings.privacy_zones_admin_enabled`` is ``True``. When disabled,
the endpoints are not registered (404 — admin surface not exposed).
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from harness.config import settings
from harness.privacy.zone_config import ZoneAction
from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

logger = logging.getLogger(__name__)

router = APIRouter()

# Reusable dependency handles (created once at import time).
_priv_read = require_scope(Scope.PRIVACY_READ)
_priv_write = require_scope(Scope.PRIVACY_WRITE)


# === Pydantic models ===

class PrivacyZone(BaseModel):
    """A privacy zone rule exposed via the REST API.

    Attributes:
        id:          UUID hex string (server-generated on create).
        pattern:     Glob pattern (e.g. ``"private/*"``, ``"**/.env"``).
        action:      ``block`` / ``redact`` / ``skip``.
        description: Optional human-readable description.
        enabled:     When ``False``, the rule is ignored at check-time.
        created_at:  UTC timestamp (server-set on create).
        updated_at:  UTC timestamp (server-set on create + each update).
    """

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=32, max_length=32)
    pattern: str = Field(min_length=1, max_length=512)
    action: Literal["block", "redact", "skip"]
    description: str | None = None
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


class PrivacyZoneCreate(BaseModel):
    """Request body for ``POST /api/v1/privacy/zones``."""

    model_config = {"extra": "forbid"}

    pattern: str = Field(min_length=1, max_length=512)
    action: Literal["block", "redact", "skip"]
    description: str | None = Field(default=None, max_length=2048)
    enabled: bool = True


class PrivacyZoneUpdate(BaseModel):
    """Request body for ``PUT /api/v1/privacy/zones/{id}``.

    All fields optional — only provided fields are updated.
    """

    model_config = {"extra": "forbid"}

    pattern: str | None = Field(default=None, min_length=1, max_length=512)
    action: Literal["block", "redact", "skip"] | None = None
    description: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None


class PrivacyZoneListResponse(BaseModel):
    """``GET /api/v1/privacy/zones`` response."""

    zones: list[PrivacyZone]
    total: int


# === In-memory store ===

class PrivacyZoneStore:
    """Thread-safe in-memory store for REST-managed privacy zones.

    State lives for the lifetime of the process (no persistence to
    SQLite — that is a Phase 5.3 follow-up). A :class:`threading.Lock`
    guards the internal dict so concurrent FastAPI requests do not
    corrupt state. The store is populated at mount time (see
    ``_ensure_store``) and shared across all requests via
    ``app.state.privacy_zone_store``.

    The zones managed here are SUPPLEMENTARY to the Settings-loaded
    rules (``privacy_zone_patterns`` + ``privacy_zone_per_action``).
    The :class:`~harness.privacy.zone_filter.PrivacyZoneFilter` checks
    Settings rules first, then REST-managed zones (or vice-versa —
    integration is left to the caller).
    """

    def __init__(self) -> None:
        self._zones: dict[str, PrivacyZone] = {}
        self._lock = threading.Lock()

    def list_zones(self) -> list[PrivacyZone]:
        """Return all zones ordered by creation time (oldest first)."""
        with self._lock:
            return sorted(
                self._zones.values(),
                key=lambda z: z.created_at,
            )

    def get(self, zone_id: str) -> PrivacyZone | None:
        """Return one zone by id, or ``None`` if not found."""
        with self._lock:
            return self._zones.get(zone_id)

    def create(
        self,
        *,
        pattern: str,
        action: ZoneAction,
        description: str | None = None,
        enabled: bool = True,
    ) -> PrivacyZone:
        """Create a new zone with a server-generated id."""
        now = datetime.now(timezone.utc)
        zone = PrivacyZone(
            id=uuid.uuid4().hex,
            pattern=pattern,
            action=action,
            description=description,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._zones[zone.id] = zone
        logger.info(
            "privacy_zone_created: id=%s pattern=%s action=%s",
            zone.id, zone.pattern, zone.action,
        )
        return zone

    def update(
        self,
        zone_id: str,
        *,
        pattern: str | None = None,
        action: ZoneAction | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> PrivacyZone | None:
        """Update an existing zone. Returns ``None`` if not found.

        ``description`` follows "replace" semantics when provided
        (``None`` means "leave unchanged"; pass empty string to clear).
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            zone = self._zones.get(zone_id)
            if zone is None:
                return None
            # Build the set of changed fields (skip ``None`` values
            # — Pydantic v2 ``model_copy(update=...)`` applies them
            # verbatim and we want "not provided" = "unchanged").
            changes: dict[str, Any] = {"updated_at": now}
            if pattern is not None:
                changes["pattern"] = pattern
            if action is not None:
                changes["action"] = action
            if description is not None:
                changes["description"] = description
            if enabled is not None:
                changes["enabled"] = enabled
            updated = zone.model_copy(update=changes)
            self._zones[zone_id] = updated
            logger.info(
                "privacy_zone_updated: id=%s fields=[%s]",
                zone_id,
                ", ".join(k for k in changes if k != "updated_at"),
            )
            return updated

    def delete(self, zone_id: str) -> bool:
        """Delete a zone by id. Returns ``True`` if it existed."""
        with self._lock:
            existed = self._zones.pop(zone_id, None) is not None
        if existed:
            logger.info("privacy_zone_deleted: id=%s", zone_id)
        return existed

    def count(self) -> int:
        """Return the number of zones (for health checks)."""
        with self._lock:
            return len(self._zones)


def _ensure_store(request: Request) -> PrivacyZoneStore:
    """Get or lazily initialise the store from ``app.state``.

    The store is set at mount time (in ``app.py`` lifespan), but we
    lazily initialise it here too so the router is testable in
    isolation without the full lifespan.
    """
    store = getattr(request.app.state, "privacy_zone_store", None)
    if store is None:
        store = PrivacyZoneStore()
        request.app.state.privacy_zone_store = store
    return store


# === Routes ===

@router.get("/zones", response_model=PrivacyZoneListResponse)
async def list_zones(
    request: Request,
    _token: Any = Depends(_priv_read),
) -> PrivacyZoneListResponse:
    """List all REST-managed privacy zones.

    Returns zones in creation order (oldest first). The response
    does NOT include Settings-loaded zones (those are visible via
    ``GET /api/v1/capabilities`` or by inspecting ``settings``).
    """
    store = _ensure_store(request)
    zones = store.list_zones()
    return PrivacyZoneListResponse(zones=zones, total=len(zones))


@router.get(
    "/zones/{zone_id}",
    response_model=PrivacyZone,
)
async def get_zone(
    zone_id: str,
    request: Request,
    _token: Any = Depends(_priv_read),
) -> PrivacyZone:
    """Get a single privacy zone by id.

    Returns 404 if the zone does not exist.
    """
    store = _ensure_store(request)
    zone = store.get(zone_id)
    if zone is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"privacy zone {zone_id!r} not found",
        )
    return zone


@router.post(
    "/zones",
    response_model=PrivacyZone,
    status_code=status.HTTP_201_CREATED,
)
async def create_zone(
    body: PrivacyZoneCreate,
    request: Request,
    _token: Any = Depends(_priv_write),
) -> PrivacyZone:
    """Create a new privacy zone.

    The server generates ``id``, ``created_at``, and ``updated_at``.
    Returns 201 on success.
    """
    store = _ensure_store(request)
    return store.create(
        pattern=body.pattern,
        action=body.action,
        description=body.description,
        enabled=body.enabled,
    )


@router.put(
    "/zones/{zone_id}",
    response_model=PrivacyZone,
)
async def update_zone(
    zone_id: str,
    body: PrivacyZoneUpdate,
    request: Request,
    _token: Any = Depends(_priv_write),
) -> PrivacyZone:
    """Update an existing privacy zone.

    Only provided fields are updated. Returns 404 if the zone does
    not exist.
    """
    store = _ensure_store(request)
    updated = store.update(
        zone_id,
        pattern=body.pattern,
        action=body.action,
        description=body.description,
        enabled=body.enabled,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"privacy zone {zone_id!r} not found",
        )
    return updated


@router.delete(
    "/zones/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_zone(
    zone_id: str,
    request: Request,
    _token: Any = Depends(_priv_write),
) -> None:
    """Delete a privacy zone.

    Returns 204 on success, 404 if the zone does not exist.
    """
    store = _ensure_store(request)
    if not store.delete(zone_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"privacy zone {zone_id!r} not found",
        )


__all__ = ["router", "PrivacyZoneStore", "PrivacyZone"]
