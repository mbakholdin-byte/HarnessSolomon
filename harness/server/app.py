"""Solomon Harness — FastAPI app factory.

Phase 0: Web MVP. Cloud-only LLM providers, 6 tools, WebSocket chat.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harness.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks."""
    # Ensure data dirs exist
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    # Init DB; rebuild from JSONL if DB is empty but JSONL has data
    from harness.server.db.sqlite import init_db, list_sessions, rebuild_from_jsonl

    await init_db()
    sessions = await list_sessions()
    if not sessions and any(settings.session_dir.glob("*.jsonl")):
        rebuilt = await rebuild_from_jsonl()
        print(f"[harness] rebuilt {rebuilt} sessions from JSONL")

    print(f"[harness] session_dir: {settings.session_dir}")
    print(f"[harness] db_path: {settings.db_path}")
    print(f"[harness] project_root: {settings.project_root}")
    yield
    # shutdown: nothing to clean up yet


def create_app() -> FastAPI:
    """Build FastAPI app with middleware and routers."""
    app = FastAPI(
        title="Solomon Harness",
        version="0.1.0",
        description="Open-source agentic shell — Web MVP (Phase 0)",
        lifespan=lifespan,
    )

    # CORS — Vite dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    from harness.server.routes.health import router as health_router
    from harness.server.routes.sessions import router as sessions_router
    from harness.server.routes.models import router as models_router

    app.include_router(health_router, prefix="/api", tags=["health"])
    app.include_router(sessions_router, prefix="/api", tags=["sessions"])
    app.include_router(models_router, prefix="/api", tags=["models"])

    return app


app = create_app()
