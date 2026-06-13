"""Health check endpoint.

GET /api/health → {status, version, project_root}
"""
from __future__ import annotations

from fastapi import APIRouter

from harness import __version__
from harness.config import settings

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {
        "status": "ok",
        "version": __version__,
        "project_root": str(settings.project_root),
    }
