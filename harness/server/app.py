"""Solomon Harness — FastAPI app factory.

Phase 0: Web MVP. Cloud-only LLM providers, 6 tools, WebSocket chat.
Phase 2.2: lifespan-level JobStore + MergeQueue singleton + the
``/api/v1/agents/jobs/...`` routes (see
:mod:`harness.server.routes.agents_jobs`).
Phase 1.6: scope-gated API — the ``TokenStore`` is initialised at
lifespan and exposed via ``app.state.token_store``. The
``app.state.auth_required`` flag mirrors ``settings.auth_required``
and lets the dependency layer short-circuit auth in dev mode
(``auth_required=False``).
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

    # Phase 2.2: instantiate the JobStore + MergeQueue singleton.
    # The JobStore lives at <db_path.parent>/agent-jobs.db (one
    # level above the sessions DB). The MergeQueue is optional —
    # if we can't construct it (e.g. no LLM router available, or
    # runner construction fails), we leave ``app.state.merge_queue``
    # as None and the routes return 503. The other routes (sessions,
    # chat) are unaffected.
    from harness.agents.jobs import JobStore
    job_store = JobStore(settings.db_path.parent / "agent-jobs.db")
    app.state.job_store = job_store
    print(f"[harness] job_store: {job_store.db_path}")

    try:
        from harness.agents.runner import AgentRunner
        from harness.agents.merge_queue import MergeQueue
        from harness.agents.verify import AdversarialVerify
        from harness.server.llm.router import LLMRouter
        router_inst = LLMRouter()
        runner = AgentRunner(router=router_inst, repo=settings.project_root)
        verifier = AdversarialVerify(runner, judges=settings.subagent_judges)
        merge_queue = MergeQueue(
            runner=runner, verifier=verifier, store=job_store,
        )
        app.state.merge_queue = merge_queue
        # recover_running() at startup (Phase 2.1) — mark in-flight
        # jobs as cancelled after a process restart.
        cancelled = await job_store.recover_running()
        if cancelled:
            print(
                f"[harness] recover_running: cancelled {len(cancelled)} job(s)"
            )
    except Exception as e:
        # LLM router construction may fail when no API keys are
        # configured. That's OK for development; the agents routes
        # will return 503, but the rest of the server works.
        print(f"[harness] merge_queue disabled (init failed: {type(e).__name__}: {e})")
        app.state.merge_queue = None

    # Phase 1.6: scope-gated API — initialise the auth token store.
    # The store lives at <db_path.parent>/harness-scope.db (sibling
    # of agent-jobs.db). Initialisation is idempotent (CREATE TABLE
    # IF NOT EXISTS), and we always succeed — there is no external
    # dependency to fail on. The ``auth_required`` flag is stashed
    # on ``app.state`` so the FastAPI dependency layer can read it
    # in O(1) without re-resolving settings on every request.
    from harness.server.auth.tokens import TokenStore
    token_store = TokenStore(settings.auth_db_path)
    await token_store.init()
    app.state.token_store = token_store
    app.state.auth_required = settings.auth_required
    print(
        f"[harness] token_store: {token_store.db_path} "
        f"(auth_required={settings.auth_required})"
    )

    print(f"[harness] session_dir: {settings.session_dir}")
    print(f"[harness] db_path: {settings.db_path}")
    print(f"[harness] project_root: {settings.project_root}")
    yield
    # shutdown: nothing to clean up yet


def create_app() -> FastAPI:
    """Build FastAPI app with middleware and routers."""
    app = FastAPI(
        title="Solomon Harness",
        version="0.6.0",
        description=(
            "Open-source agentic shell — Web MVP (Phase 0) + "
            "sub-agent system (Phase 2.0+2.1) + GitHub PR integration (Phase 2.2) "
            "+ scope-gated API (Phase 1.6)"
        ),
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
    from harness.server.routes.chat import router as chat_router
    from harness.server.routes.agents_jobs import router as agents_jobs_router
    from harness.server.routes.capabilities import router as capabilities_router
    from harness.server.routes.memory_v1 import router as memory_v1_router
    from harness.server.routes.sessions_v1 import router as sessions_v1_router

    app.include_router(health_router, prefix="/api", tags=["health"])
    app.include_router(sessions_router, prefix="/api", tags=["sessions"])
    app.include_router(models_router, prefix="/api", tags=["models"])
    app.include_router(chat_router, prefix="/api/chat")  # WebSocket only
    # Phase 2.2: merge-queue HTTP API. Phase 1.6: routes now require
    # ``agents.read`` via ``Depends(require_scope(Scope.AGENTS_READ))``.
    app.include_router(agents_jobs_router, prefix="/api/v1/agents", tags=["agents"])
    # Phase 1.6: capabilities discovery — always public so a client
    # with no token can still discover the server's auth surface.
    app.include_router(
        capabilities_router, prefix="/api/v1", tags=["capabilities"],
    )
    # Phase 1.6: memory + sessions v1 routes (scope-gated).
    app.include_router(
        memory_v1_router, prefix="/api/v1/memory", tags=["memory"],
    )
    app.include_router(
        sessions_v1_router, prefix="/api/v1/sessions", tags=["sessions-v1"],
    )

    return app


app = create_app()
