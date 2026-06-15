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
        from harness.agents.outbound import (
            OutboundWebhookDispatcher, parse_urls,
        )
        from harness.server.llm.router import LLMRouter
        router_inst = LLMRouter()
        runner = AgentRunner(router=router_inst, repo=settings.project_root)
        verifier = AdversarialVerify(runner, judges=settings.subagent_judges)
        # Phase 2.5: outbound dispatcher (singleton). Constructed
        # eagerly at lifespan so the ``MergeQueue`` and
        # ``WebhookHandler`` below can both inject it. Empty
        # ``urls`` → outbound is a no-op (default behaviour, no
        # network calls).
        outbound = OutboundWebhookDispatcher(
            urls=parse_urls(settings.outbound_webhook_urls),
            token=settings.outbound_webhook_token,
            timeout_s=settings.outbound_webhook_timeout_s,
            max_retries=settings.outbound_webhook_max_retries,
        )
        app.state.outbound = outbound
        if parse_urls(settings.outbound_webhook_urls):
            print(
                f"[harness] outbound: enabled "
                f"({len(parse_urls(settings.outbound_webhook_urls))} url(s))"
            )
        else:
            print("[harness] outbound: disabled (no urls configured)")
        merge_queue = MergeQueue(
            runner=runner, verifier=verifier, store=job_store,
            outbound=outbound,
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

    # Phase 3: instantiate the optional ``ContextCompactor``. The
    # compactor is wired into ``AgentLoop`` (set in ``chat.py`` and
    # any other caller that constructs an ``AgentLoop`` per-request)
    # via ``app.state.compactor``. It is **not** injected here into
    # ``MergeQueue`` / ``WebhookHandler`` — those work with full
    # prompts and don't need sliding-window compaction. Construction
    # is best-effort: if the LLM router is unavailable we leave
    # ``app.state.compactor = None`` so the chat path remains a
    # no-op (pre-Phase-3 behaviour).
    from harness.context.compaction import ContextCompactor
    try:
        if settings.compaction_enabled:
            from harness.server.llm.router import LLMRouter
            compactor_router = LLMRouter()
            compactor = ContextCompactor(
                settings=settings,
                router=compactor_router,
                memory=None,  # Phase 3.5 will wire UnifiedMemory
            )
            app.state.compactor = compactor
            print(
                f"[harness] compactor: enabled "
                f"(summariser={settings.compaction_summarizer_model or settings.subagent_t1_model}, "
                f"threshold={settings.compaction_threshold_ratio}, "
                f"target={settings.compaction_target_ratio})"
            )
        else:
            app.state.compactor = None
            print("[harness] compactor: disabled")
    except Exception as e:
        # LLM router construction may fail when no API keys are set.
        # Compaction is non-critical — leave disabled.
        print(
            f"[harness] compactor disabled (init failed: "
            f"{type(e).__name__}: {e})"
        )
        app.state.compactor = None

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

    # Phase 2.3: inbound GitHub webhook receiver. The
    # ``WebhookEventStore`` lives in the same DB file as the
    # ``JobStore`` (``agent-jobs.db``) but in a different table
    # (``webhook_events``). The ``WebhookHandler`` wraps the store
    # + the HMAC secret and is what the route calls. If
    # ``webhook_secret`` is empty, the route returns 503 — but the
    # rest of the server (sessions, chat, agents/jobs) is
    # unaffected. Setting the secret to a non-empty value at
    # runtime requires a server restart.
    from harness.agents.webhook_handler import WebhookHandler
    from harness.agents.webhook_store import WebhookEventStore
    try:
        webhook_event_store = WebhookEventStore(
            settings.db_path.parent / "agent-jobs.db",
        )
        await webhook_event_store.init()
        app.state.webhook_event_store = webhook_event_store
        # Phase 2.4: inject the merger / auto_merger callables so
        # the dispatcher can act on ``pull_request_review.approved``
        # events. We import the actual functions from
        # :mod:`harness.agents.pr_integration` at this point (DI
        # keeps :mod:`harness.agents.webhook_handler` free of
        # those imports at module top level — the trust boundary).
        from harness.agents.pr_integration import (
            enable_auto_merge,
            merge_pr,
        )
        app.state.webhook_handler = WebhookHandler(
            store=webhook_event_store,
            secret=settings.webhook_secret,
            merger=merge_pr,
            auto_merger=enable_auto_merge,
            outbound=outbound,
        )
        # Log whether webhooks are enabled or not (without
        # leaking the secret itself).
        secret_configured = bool(
            settings.webhook_secret and settings.webhook_secret.strip()
        )
        print(
            f"[harness] webhook_event_store: {webhook_event_store.db_path} "
            f"(webhooks={'enabled' if secret_configured else 'disabled'})"
        )
    except Exception as e:
        # Webhook store init failure is non-fatal — the rest of
        # the server continues to work, and the route returns 503.
        print(
            f"[harness] webhook handler disabled (init failed: "
            f"{type(e).__name__}: {e})"
        )
        app.state.webhook_event_store = None
        app.state.webhook_handler = None

    print(f"[harness] session_dir: {settings.session_dir}")
    print(f"[harness] db_path: {settings.db_path}")
    print(f"[harness] project_root: {settings.project_root}")
    yield
    # shutdown: close the outbound dispatcher's HTTP client.
    # ``outbound`` may not exist if the runner init failed (in
    # which case ``app.state.outbound`` was never set); guard
    # with a default-attr lookup.
    outbound = getattr(app.state, "outbound", None)
    if outbound is not None:
        await outbound.aclose()


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
    from harness.server.routes.agents_webhooks import router as agents_webhooks_router

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
    # Phase 2.3: inbound GitHub webhook receiver. Mounted at the
    # operator-configurable path (``settings.webhook_path``). Default
    # is ``/api/v1/agents/webhooks/github``. The router itself
    # exposes ``/webhooks/github``; we prefix it with the part of
    # the path BEFORE ``/webhooks/github`` so the final URL is
    # exactly ``settings.webhook_path``. (FastAPI's ``include_router``
    # with a dynamic ``prefix`` would need a workaround; here we
    # use the default prefix and assume the operator doesn't
    # override the path — we log a warning if they do.)
    configured_path = settings.webhook_path
    expected_prefix = configured_path.rsplit("/webhooks/", 1)[0]
    if expected_prefix == configured_path:
        # No ``/webhooks/`` suffix → fall back to the default
        # prefix. This is the rare case where the operator
        # changed the path; we log and use the default.
        print(
            f"[harness] webhook_path={configured_path!r} does not end "
            f"with /webhooks/...; mounting at default /api/v1/agents"
        )
        expected_prefix = "/api/v1/agents"
    app.include_router(
        agents_webhooks_router, prefix=expected_prefix, tags=["webhooks"],
    )

    return app


app = create_app()
