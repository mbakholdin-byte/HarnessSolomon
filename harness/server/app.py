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
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harness.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks."""
    # Phase 4.4+ v1.14.0: SessionStart / SessionEnd emission points.
    import time as _time
    _startup_ts = _time.monotonic()

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
        # Phase 3 v1.5.0: build the PrivacyZoneFilter and pass it
        # to AgentRunner (privacy_zones=kwarg). Mirrors the
        # scratchpad_audit / offloader_factory / reflection_factory
        # pattern: we import :mod:harness.privacy here (lifespan
        # scope) so the runner never has to.
        from harness.privacy import PrivacyZoneFilter, parse_zones

        _privacy_rules = parse_zones(
            patterns_str=settings.privacy_zone_patterns,
            per_action_str=settings.privacy_zone_per_action,
            default_action=settings.privacy_zone_default_action,
        )
        # Create a local ScratchpadAudit sink (gated by
        # privacy_zones_audit_log). Mirrors the audit pattern used
        # by the offloader / reflection / compact modules in Phase 3
        # v1.3.1+ — operators opt in via env var. Always-constructed
        # (cheap: a no-op writer) so the reference is always stable.
        from harness.context.scratchpad_audit import ScratchpadAudit
        _privacy_audit = (
            ScratchpadAudit(enabled=True) if settings.privacy_zones_audit_log else None
        )
        privacy_zones_filter = PrivacyZoneFilter(
            rules=_privacy_rules,
            audit=_privacy_audit,
            enabled=settings.privacy_zones_enabled,
        )
        app.state.privacy_zones = privacy_zones_filter
        print(
            f"[harness] privacy_zones: enabled={settings.privacy_zones_enabled} "
            f"rules={len(_privacy_rules)} audit={settings.privacy_zones_audit_log}"
        )
        from harness.server.llm.router import LLMRouter
        router_inst = LLMRouter()
        runner = AgentRunner(
            router=router_inst,
            repo=settings.project_root,
            privacy_zones=privacy_zones_filter,
        )
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
    # Phase 3.5: also wire ``CompactStore`` (persistent compact
    # cache) and ``UnifiedMemory`` (closes the Phase 3.5 planned
    # hook on line 117 of the previous version). Both are
    # best-effort — if they fail to init, the compactor still
    # works in pure in-memory mode.
    from harness.context.compaction import ContextCompactor
    try:
        if settings.compaction_enabled:
            from harness.server.llm.router import LLMRouter
            compactor_router = LLMRouter()
            # Phase 3 v1.5.0: build the pre_compact_hook closure
            # that the compactor will call before each slow-path
            # run. Imports :mod:`harness.agents.pre_compact` here
            # (lifespan scope) so the compactor never has to.
            def _build_pre_compact_hook(unified_memory: Any) -> Any:
                from harness.agents.pre_compact import PreCompactHook
                return PreCompactHook(
                    memory=unified_memory,
                    settings=settings,
                )
            # Phase 3.5: optional UnifiedMemory injection. Used for
            # the L2 #compact tag mirror (cross-session retrieval of
            # compaction summaries). The compactor treats memory as
            # a write-only mirror — a failure here is logged but
            # never aborts the chat loop.
            unified_memory = None
            try:
                from harness.memory.unified import UnifiedMemory
                unified_memory = UnifiedMemory(
                    settings=settings,
                    db_path=settings.db_path.parent / "memory.db",
                )
                print(f"[harness] unified_memory: {settings.db_path.parent / 'memory.db'}")
            except Exception as mem_exc:
                print(
                    f"[harness] unified_memory disabled (init failed: "
                    f"{type(mem_exc).__name__}: {mem_exc})"
                )
                unified_memory = None
            app.state.unified_memory = unified_memory
            # Phase 3.5: optional CompactStore. Used for the
            # persistent compact cache (skip LLM summariser on
            # reconnect). Compactor is the sole consumer; the
            # store is a private dependency that lives only as
            # long as the app is running.
            compact_store = None
            try:
                from harness.agents.compact_store import CompactStore
                persistent_store = getattr(
                    settings, "compaction_persistent_store", True,
                )
                if persistent_store:
                    compact_store = CompactStore(
                        settings.db_path.parent / "agent-jobs.db",
                    )
                    await compact_store.init()
                    print(
                        f"[harness] compact_store: "
                        f"{settings.db_path.parent / 'agent-jobs.db'}"
                    )
                else:
                    print("[harness] compact_store: disabled by setting")
            except Exception as store_exc:
                print(
                    f"[harness] compact_store disabled (init failed: "
                    f"{type(store_exc).__name__}: {store_exc})"
                )
                compact_store = None
            app.state.compact_store = compact_store
            # Phase 3 v1.5.0: optional time/turn/hybrid trigger. The
            # trigger is constructed here (lifespan scope) and
            # passed to the compactor; the compactor only sees a
            # duck-typed handle so it never imports the idle_trigger
            # module. The trigger is a no-op (returns False) when
            # ``compaction_trigger == "token"`` (default) — the
            # classic token-threshold behaviour is preserved.
            idle_trigger = None
            try:
                trigger_mode = getattr(
                    settings, "compaction_trigger", "token",
                )
                if trigger_mode in ("turn", "time", "hybrid"):
                    from harness.agents.idle_trigger import (
                        TimeBasedCompactionTrigger,
                    )
                    idle_trigger = TimeBasedCompactionTrigger(settings=settings)
                    print(
                        f"[harness] idle_trigger: mode={trigger_mode} "
                        f"(turn_interval="
                        f"{getattr(settings, 'compaction_turn_interval', 20)}, "
                        f"idle_minutes="
                        f"{getattr(settings, 'compaction_time_idle_minutes', 30)})"
                    )
            except Exception as trigger_exc:
                print(
                    f"[harness] idle_trigger disabled (init failed: "
                    f"{type(trigger_exc).__name__}: {trigger_exc})"
                )
                idle_trigger = None
            # Wire everything into the compactor.
            # Phase 3.5: optional audit writer. The audit log is
            # best-effort and opt-in via ``compaction_audit_log``;
            # when disabled, ``record()`` is a no-op so the audit
            # writer has zero overhead.
            from harness.context.compaction_audit import CompactionAudit
            audit = CompactionAudit(
                audit_dir=settings.session_dir / "audit",
                enabled=getattr(settings, "compaction_audit_log", False),
            )
            compactor = ContextCompactor(
                settings=settings,
                router=compactor_router,
                memory=unified_memory,
                store=compact_store,
                audit=audit,
                pre_compact_hook=(
                    _build_pre_compact_hook(unified_memory)
                    if settings.pre_compact_enabled
                    else None
                ),
                idle_trigger=idle_trigger,
            )
            app.state.compactor = compactor
            print(
                f"[harness] compactor: enabled "
                f"(summariser={settings.compaction_summarizer_model or settings.subagent_t1_model}, "
                f"threshold={settings.compaction_threshold_ratio}, "
                f"target={settings.compaction_target_ratio}, "
                f"store={'yes' if compact_store else 'no'}, "
                f"memory={'yes' if unified_memory else 'no'})"
            )
        else:
            app.state.compactor = None
            app.state.compact_store = None
            app.state.unified_memory = None
            print("[harness] compactor: disabled")
    except Exception as e:
        # LLM router construction may fail when no API keys are set.
        # Compaction is non-critical — leave disabled.
        print(
            f"[harness] compactor disabled (init failed: "
            f"{type(e).__name__}: {e})"
        )
        app.state.compactor = None
        app.state.compact_store = None
        app.state.unified_memory = None

    # Phase 3 v1.4.0: wire the ``CompactTrigger`` for manual /compact.
    # The trigger wraps the compactor with per-call timeout + audit
    # so the HTTP route, CLI subcommand, and WebSocket message
    # handler can all share the same failure semantics. If the
    # compactor was disabled at init we still create a trigger
    # pointed at ``None`` so the /compact route can return a clean
    # 503 rather than 500-ing on AttributeError.
    from harness.server.agent.compact_trigger import CompactTrigger
    audit = getattr(app.state, "compactor", None)
    if audit is not None:
        # The trigger wants the same audit writer the compactor uses.
        try:
            compactor_audit = getattr(audit, "_audit", None)
        except Exception:
            compactor_audit = None
    else:
        compactor_audit = None
    app.state.compact_trigger = CompactTrigger(
        compactor=getattr(app.state, "compactor", None),
        settings=settings,
        audit=compactor_audit,
    )
    if app.state.compact_trigger._compactor is not None:
        print("[harness] compact_trigger: enabled")
    else:
        print("[harness] compact_trigger: disabled (no compactor)")

    # Phase 3 v1.4.0: build the ``reflection_factory`` closure for
    # the runner. The factory constructs a ``ReflectionLoop`` on
    # demand per-session. The closure keeps ``app.state.unified_memory``
    # and the audit writer in scope without leaking them into the
    # runner module (trust boundary — the runner only sees a
    # ``Callable[..., Any]``).
    def _reflection_factory(*, spec: Any, session_id: str,
                            scratchpad: Any = None) -> Any:
        """Closure: build a ReflectionLoop with lifespan-bound deps.

        Mirrors the ``offloader_factory`` / ``scratchpad_factory``
        pattern from Phase 3 v1.3.1 / v1.2.0. We import the
        reflection module here (lifespan scope) so the runner never
        has to.
        """
        from harness.server.agent.reflection_loop import ReflectionLoop
        return ReflectionLoop(
            scratchpad=scratchpad,
            settings=settings,
            router=router_inst if "router_inst" in dir() else None,
            unified_memory=getattr(app.state, "unified_memory", None),
            audit=compactor_audit,
        )

    app.state.reflection_factory = _reflection_factory
    print("[harness] reflection_factory: enabled (closure ready)")

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

    # Phase 4.2: hot-reload watchers for .harness/agents/*.md,
    # .harness/hooks/*.json, and (Phase 4.2+) .harness/privacy/*.json.
    # Best-effort — if any fails, log and continue (the app works
    # without hot-reload).
    app.state.hot_reload_watcher = None
    if settings.hot_reload_enabled:
        try:
            from harness.agents.hot_reload import (
                start_agent_hot_reload,
                start_builtin_agent_hot_reload,
            )
            from harness.hooks.hot_reload import start_hook_hot_reload
            from harness.privacy.hot_reload import start_privacy_hot_reload
            from harness.watcher import get_file_watcher

            # Use the SAME singleton for all watchers. Calling
            # get_file_watcher() returns the cached instance.
            # Phase 4.2+ v1.9.0: also watch built-in agents
            # (harness/agents/builtin/*.md) for dev iteration.
            await start_builtin_agent_hot_reload(
                debounce_ms=settings.hot_reload_debounce_ms,
            )
            await start_agent_hot_reload(
                settings.project_root,
                debounce_ms=settings.hot_reload_debounce_ms,
            )
            # Hook registry may or may not exist (Phase 4.0 + 2.x).
            # If it does, wire the hooks watcher.
            hook_registry = getattr(app.state, "hook_registry", None)
            if hook_registry is not None:
                await start_hook_hot_reload(
                    hook_registry,
                    settings.project_root,
                    debounce_ms=settings.hot_reload_debounce_ms,
                )
            # PrivacyZoneFilter may or may not exist (Phase 3 v1.5.0).
            # If it does, wire the privacy watcher.
            privacy_zones_filter = getattr(app.state, "privacy_zones", None)
            if privacy_zones_filter is not None:
                await start_privacy_hot_reload(
                    privacy_zones_filter,
                    settings.project_root,
                    default_action=settings.privacy_zone_default_action,
                    debounce_ms=settings.hot_reload_debounce_ms,
                )
            app.state.hot_reload_watcher = get_file_watcher()
            print(
                f"[harness] hot_reload: enabled "
                f"(debounce={settings.hot_reload_debounce_ms}ms)"
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            print(
                f"[harness] hot_reload: disabled (init failed: "
                f"{type(exc).__name__}: {exc})"
            )
    else:
        print("[harness] hot_reload: disabled (settings.hot_reload_enabled=False)")

    # Phase 4.4+ v1.14.0: wire the global HookRunner singleton to the
    # SAME registry that the server's DI runner uses. This is the
    # collapse from the dual-registry risk in the v1.14.0 design
    # review (Blocker #3): production emission sites that cannot
    # DI (UnifiedMemory, LLMRouterClassifier, ContextCompactor)
    # call ``safe_fire()`` which delegates to the global runner.
    # If the global runner is None, ``safe_fire`` still works (it
    # lazy-initializes from ``get_registry()``); we set it
    # explicitly here so the registry is the SAME object the DI
    # runner uses (no parallel registries).
    try:
        from harness.hooks.registry import get_registry
        from harness.hooks.runner import (
            HookRunner,
            set_global_hook_runner,
        )
        server_registry = get_registry()
        server_runner = HookRunner(server_registry, default_timeout_ms=3000)
        app.state.hook_runner = server_runner
        set_global_hook_runner(server_runner)
        print(
            f"[harness] hook_runner: enabled "
            f"(registry_size={len(server_registry)})"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(
            f"[harness] hook_runner: disabled (init failed: "
            f"{type(exc).__name__}: {exc})"
        )

    # Phase 4.4+ v1.14.0: SessionStart — process-level, NOT per-session (#9 review)
    try:
        from harness.hooks.runner import safe_fire
        await safe_fire(
            "SessionStart",
            payload={
                "session_id": "server-boot",
                "working_dir": str(settings.project_root),
            },
        )
    except Exception:  # noqa: BLE001 — best-effort
        pass

    yield
    # shutdown: close the outbound dispatcher's HTTP client.
    # ``outbound`` may not exist if the runner init failed (in
    # which case ``app.state.outbound`` was never set); guard
    # with a default-attr lookup.
    outbound = getattr(app.state, "outbound", None)
    if outbound is not None:
        await outbound.aclose()
    # Phase 4.4+ v1.14.0: SessionEnd — best-effort, shutdown must not hang
    try:
        from harness.hooks.runner import safe_fire
        await safe_fire(
            "SessionEnd",
            payload={
                "session_id": "server-boot",
                "duration_seconds": round(_time.monotonic() - _startup_ts, 1),
            },
        )
    except Exception:  # noqa: BLE001 — best-effort
        pass
    # Phase 4.2: stop hot-reload watcher.
    watcher = getattr(app.state, "hot_reload_watcher", None)
    if watcher is not None:
        await watcher.stop()


def create_app() -> FastAPI:
    """Build FastAPI app with middleware and routers."""
    app = FastAPI(
        title="Solomon Harness",
        version="1.18.0",
        description=(
            "Open-source agentic shell — Web MVP (Phase 0) + "
            "sub-agent system (Phase 2.0+2.1) + GitHub PR integration (Phase 2.2) "
            "+ scope-gated API (Phase 1.6) + observability (Phase 4.1).\n\n"
            "**API versioning:** Legacy `/api/*` paths return RFC 8594 "
            "`Deprecation: true` and `Sunset: Wed, 31 Dec 2026 23:59:59 GMT` "
            "headers. New clients SHOULD use `/api/v1/*` (canonical). "
            "See docs/api-versioning.md for the migration timeline."
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

    # Phase 4.1+ Step 1: Legacy /api/* deprecation headers (RFC 8594 + 8288).
    # MUST come BEFORE the observability middleware so deprecation headers
    # are visible in /metrics scrapes and the JSONL log lines.
    from harness.server.deprecation import install_deprecation_middleware
    install_deprecation_middleware(app)

    # Phase 4.1 Step 6.2: HTTP request metrics + structured logging
    from harness.server.middleware import install_observability_middleware
    install_observability_middleware(app)

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
    # Phase 4.1+ Step 2: dual-mount health_router at /api/v1 (canonical).
    # Legacy /api/health is already covered by the deprecation middleware
    # in routes/observability.py. The /api/v1 mount below is the canonical
    # successor for any clients that want the versioned path.
    app.include_router(health_router, prefix="/api/v1", tags=["health"])
    # Phase 4.1 Step 6.11: /metrics + /health/* (no prefix — top-level endpoints)
    from harness.server.routes.observability import router as observability_router
    app.include_router(observability_router, tags=["observability"])
    # Phase 4.1+ Step 3: legacy sessions_router at /api (deprecation
    # headers). sessions_v1_router is already at /api/v1/sessions and
    # is the canonical successor — see include_router below.
    app.include_router(sessions_router, prefix="/api", tags=["sessions"])
    # Phase 4.1+ Step 4: legacy models_router at /api (deprecation headers).
    # No /api/v1/models router exists yet — the legacy paths are the only
    # /api/v1/models successor. We mount the same router at /api/v1 with
    # an empty prefix (the router itself defines /models in its path).
    app.include_router(models_router, prefix="/api", tags=["models"])
    app.include_router(models_router, prefix="/api/v1", tags=["models"])
    # Phase 4.1+ Step 5: dual-mount chat WebSocket at /api/chat + /api/v1/chat.
    # WebSocket upgrade responses are handled by the deprecation middleware
    # (it wraps both HTTP and WS responses).
    app.include_router(chat_router, prefix="/api/chat")  # WebSocket only
    app.include_router(chat_router, prefix="/api/v1/chat")  # canonical
    # Phase 4.3+ v1.12.0: Elicitation WebSocket — canonical only (no legacy
    # mount; the feature is new and the deprecation timeline is fresh).
    from harness.server.routes.elicitation import router as elicitation_router
    app.include_router(
        elicitation_router, prefix="/api/v1/elicitation", tags=["elicitation"],
    )
    # Phase 4.3+ v1.15.0: HTTP long-poll fallback for Elicitation.
    # Mounted unconditionally (the router enforces the
    # ``hooks_elicitation_longpoll_enabled`` flag per-request, returning
    # 403 when disabled) so operators can flip the flag at runtime
    # without restarting the server. The default is False — WS-first
    # policy. Conditional import mirrors the WS router pattern.
    from harness.server.routes.elicitation_longpoll import (
        router as elicitation_longpoll_router,
    )
    app.state.hooks_elicitation_longpoll_enabled = (
        settings.hooks_elicitation_longpoll_enabled
    )
    app.state.hooks_elicitation_longpoll_timeout_s = (
        settings.hooks_elicitation_longpoll_timeout_s
    )
    app.state.hooks_elicitation_longpoll_interval_s = (
        settings.hooks_elicitation_longpoll_interval_s
    )
    app.include_router(
        elicitation_longpoll_router,
        prefix="/api/v1/elicitation",
        tags=["elicitation-longpoll"],
    )
    print(
        f"[harness] elicitation_longpoll: "
        f"{'enabled' if settings.hooks_elicitation_longpoll_enabled else 'disabled'} "
        f"(timeout={settings.hooks_elicitation_longpoll_timeout_s}s, "
        f"interval={settings.hooks_elicitation_longpoll_interval_s}s)"
    )
    # Phase 4.8 v1.18.0: Elicitation decision history endpoint.
    # Read-only view over the shared ``agent-jobs.db`` SQLite file. No
    # enable flag — the table is created lazily by
    # ElicitationDecisionStore on first open and the endpoint returns
    # an empty array when no decisions have been recorded yet.
    from harness.server.routes.elicitation_history import (
        router as elicitation_history_router,
    )
    app.state.elicitation_decision_db_path = (
        settings.db_path.parent / "agent-jobs.db"
    )
    app.include_router(
        elicitation_history_router,
        prefix="/api/v1/elicitation",
        tags=["elicitation-history"],
    )
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
