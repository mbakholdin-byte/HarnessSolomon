"""Solomon Harness — entry point.

Usage:
    python -m harness

Starts the FastAPI server on HARNESS_HOST:HARNESS_PORT (default 0.0.0.0:8000).
"""
from __future__ import annotations

import uvicorn

from harness.config import settings


def main() -> None:
    """Run uvicorn with our FastAPI app."""
    uvicorn.run(
        "harness.server.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
