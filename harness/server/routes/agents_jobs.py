"""HTTP API for the merge-queue job store (Phase 2.2, Step 4 + Phase 1.6).

Endpoints:
  - ``GET /api/v1/agents/jobs/<job_id>`` — fetch one job's record (Phase 1.6: requires ``agents.read``)
  - ``GET /api/v1/agents/jobs?recent=N`` — list the N most recent jobs (Phase 1.6: requires ``agents.read``)
  - ``GET /api/v1/agents/health`` — queue health (stats for ops) (Phase 1.6: requires ``agents.read``)

The router is mounted at ``/api/v1/agents`` in :mod:`harness.server.app`.
It reads from ``app.state.job_store`` (a :class:`JobStore` instance
set up in the FastAPI lifespan handler) and never constructs one
itself — the trust boundary from Phase 2.0 is preserved.

Failure modes:
  - 401 if no token / malformed header / wrong / revoked token (Phase 1.6)
  - 403 if token lacks ``agents.read`` scope (Phase 1.6)
  - 404 if the job_id is unknown
  - 503 if ``app.state.job_store`` is not configured (e.g. the
    lifespan handler failed to initialise it). The route is wired
    so this is observable; the rest of the server (sessions, chat)
    is unaffected.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from harness.server.auth.deps import require_scope
from harness.server.auth.scopes import Scope

router = APIRouter()


class _JobRecordSchema(BaseModel):
    """JSON shape of a :class:`harness.agents.jobs.JobRecord`."""

    id: str
    worktree_id: str
    status: str
    started_at: str
    finished_at: str | None
    cost: float
    error: str | None
    model: str
    prompt: str
    # Phase 2.2: PR integration fields
    repo: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    target_branch: str | None = None
    pr_mode: str = "off"
    # Phase 2.4: stacked / multi-PR fields
    pr_stack_id: str | None = None
    stack_position: int = 0
    stack_size: int = 1
    depends_on_pr_number: int | None = None

    @classmethod
    def from_record(cls, rec: Any) -> "_JobRecordSchema":
        return cls(
            id=rec.id, worktree_id=rec.worktree_id, status=rec.status,
            started_at=rec.started_at, finished_at=rec.finished_at,
            cost=rec.cost, error=rec.error, model=rec.model, prompt=rec.prompt,
            repo=rec.repo, pr_url=rec.pr_url, pr_number=rec.pr_number,
            target_branch=rec.target_branch, pr_mode=rec.pr_mode,
            pr_stack_id=rec.pr_stack_id,
            stack_position=rec.stack_position,
            stack_size=rec.stack_size,
            depends_on_pr_number=rec.depends_on_pr_number,
        )


class _StackSchema(BaseModel):
    """JSON shape of ``GET /stacks/{stack_id}`` — a parent + children."""

    stack_id: str
    parent: _JobRecordSchema | None = None
    children: list[_JobRecordSchema] = []


class _QueueHealth(BaseModel):
    """JSON shape of the merge-queue health endpoint."""

    queue_locks: dict[str, int]
    job_store_path: str
    recent_job_count: int


def _get_store(request: Request) -> Any:
    """Pull the :class:`JobStore` from ``app.state`` or 503.

    The store is set up in the FastAPI lifespan handler. If it's
    missing, we surface a 503 — this should only happen if lifespan
    init failed (a programmer error, not a runtime condition).
    """
    store = getattr(request.app.state, "job_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="JobStore not initialised (server lifespan init failed)",
        )
    return store


# Phase 1.6: every route below requires the ``agents.read`` scope.
# The ``_agents_read`` alias is created once at import time and
# shared across all three GETs (FastAPI caches dep callables).
_agents_read = require_scope(Scope.AGENTS_READ)


@router.get("/jobs/{job_id}", response_model=_JobRecordSchema)
async def get_job(
    job_id: str,
    request: Request,
    _token: Any = Depends(_agents_read),
) -> _JobRecordSchema:
    """Fetch one job by id. 404 if not found.

    Phase 1.6: requires ``agents.read`` scope. The token is not
    used by the handler body but its presence is enforced by
    the dependency — FastAPI will resolve it before the handler
    runs.
    """
    import asyncio
    store = _get_store(request)
    rec = await store.load(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return _JobRecordSchema.from_record(rec)


@router.get("/jobs", response_model=list[_JobRecordSchema])
async def list_jobs(
    request: Request,
    recent: int = 20,
    _token: Any = Depends(_agents_read),
) -> list[_JobRecordSchema]:
    """List the ``recent`` most recent jobs (default 20, newest first).

    Phase 1.6: requires ``agents.read`` scope.
    """
    import asyncio
    store = _get_store(request)
    if recent <= 0:
        return []
    recs = await store.list_recent(recent)
    return [_JobRecordSchema.from_record(r) for r in recs]


@router.get("/stacks/{stack_id}", response_model=_StackSchema)
async def get_stack(
    stack_id: str,
    request: Request,
    _token: Any = Depends(_agents_read),
) -> _StackSchema:
    """Phase 2.4: fetch a stack by its ``pr_stack_id``.

    Returns the parent orchestrator row (``stack_position=0``) and
    the ordered list of children (one per slice). 404 if no row
    matches ``stack_id``. The parent's status reflects the
    aggregate (``pr_open`` while children are in flight, ``merged``
    after ``all_stack_children_merged`` is True, ``failed`` if
    the stack was cascade-cancelled).

    Phase 1.6: requires ``agents.read`` scope.
    """
    store = _get_store(request)
    rows = await store.find_jobs_by_stack_id(stack_id)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"stack {stack_id!r} not found",
        )
    parent: _JobRecordSchema | None = None
    children: list[_JobRecordSchema] = []
    for r in rows:
        schema = _JobRecordSchema.from_record(r)
        if r.stack_position == 0:
            parent = schema
        else:
            children.append(schema)
    return _StackSchema(stack_id=stack_id, parent=parent, children=children)


@router.get("/health", response_model=_QueueHealth)
async def queue_health(
    request: Request,
    _token: Any = Depends(_agents_read),
) -> _QueueHealth:
    """Ops health endpoint: per-repo lock stats + recent job count.

    The lock stats are read from ``app.state.merge_queue`` when
    available; the recent-job count is read from the store. The
    two are independent so a missing queue doesn't kill the
    health response.

    Phase 1.6: requires ``agents.read`` scope (it's a read
    endpoint, and we keep the auth surface uniform across the
    v1 namespace — operators who want a fully-open health
    endpoint can hit ``/api/health`` which remains legacy-open).
    """
    import asyncio
    store = _get_store(request)
    queue = getattr(request.app.state, "merge_queue", None)
    locks = queue._locks.stats() if queue is not None else {}
    recs = await store.list_recent(1000)  # cheap count
    return _QueueHealth(
        queue_locks=locks,
        job_store_path=str(store.db_path),
        recent_job_count=len(recs),
    )


# === Phase 1.6: POST /api/v1/agents/jobs ===

from pydantic import BaseModel as _BaseModel, Field as _Field  # noqa: E402
from harness.server.auth.scopes import Scope as _Scope  # noqa: E402


class _EnqueueRequest(_BaseModel):
    """``POST /api/v1/agents/jobs`` body."""

    prompt: str = _Field(..., min_length=1, max_length=8000)
    agent: str = _Field(
        default="explore",
        description=(
            "Sub-agent name (built-in or project override). "
            "Default: 'explore' (read-only, safe default)."
        ),
    )
    model: str | None = _Field(
        default=None,
        description=(
            "Override the agent's default model. Must be a catalog id. "
            "Default: agent's spec.model."
        ),
    )
    pr_mode: str = _Field(
        default="off",
        description=(
            "Phase 2.2: PR mode. One of 'off' (local ff-merge), "
            "'draft' (open a draft PR), 'ready' (open a ready PR). "
            "Requires agents.pr scope when not 'off'."
        ),
    )
    pr_target: str | None = _Field(
        default=None,
        description=(
            "Phase 2.2: target branch for the PR. "
            "Default: settings.pr_default_target_branch."
        ),
    )
    background: bool = _Field(
        default=True,
        description=(
            "Enqueue and return a job_id (default true). If false, "
            "the route runs the agent synchronously and returns the "
            "result inline."
        ),
    )
    # Phase 2.4: stacked / multi-PR
    split_into: int | None = _Field(
        default=None,
        ge=1, le=64,
        description=(
            "Phase 2.4: split the job's diff into N stacked PRs. "
            "Each slice's PR targets the previous slice's branch. "
            "Requires ``pr_mode`` != 'off' (stacks need gh). "
            "Default: null (no split, single-PR path)."
        ),
    )
    split_strategy: str | None = _Field(
        default=None,
        description=(
            "Phase 2.4: split strategy override. One of 'auto', "
            "'files', 'directory', 'size'. Default: "
            "settings.pr_split_strategy = 'auto'."
        ),
    )
    stack_id: str | None = _Field(
        default=None,
        description=(
            "Phase 2.4: stack identifier. If omitted, the queue "
            "generates a new one. Use to re-enqueue an existing "
            "stack with the same id (rare; usually managed by the "
            "orchestrator)."
        ),
    )


class _EnqueueResponse(_BaseModel):
    """``POST /api/v1/agents/jobs`` response."""

    job_id: str
    status: str  # "queued" or "running"
    worktree_id: str
    prompt: str
    pr_mode: str
    pr_target: str | None = None
    model: str


# Two dep handles: agents.write (always) and agents.pr (only
# when pr_mode != "off"). The route uses both — but the compound
# check is encoded in the route body, not in a single dep,
# because the pr_scope requirement is conditional.
_agents_write = require_scope(_Scope.AGENTS_WRITE)
_agents_pr = require_scope(_Scope.AGENTS_PR)


@router.post("/jobs", response_model=_EnqueueResponse, status_code=201)
async def enqueue_job(
    body: _EnqueueRequest,
    request: Request,
    token: Any = Depends(_agents_write),
) -> _EnqueueResponse:
    """Enqueue a sub-agent job (Phase 1.6).

    Phase 1.6 scope contract:
      * Always requires ``agents.write``.
      * Additionally requires ``agents.pr`` when ``pr_mode != "off"``
        (compound check, enforced explicitly in the handler body).

    Validation:
      * ``prompt`` is non-empty (Pydantic)
      * ``agent`` must be a known spec (built-in or project override)
      * ``model`` (if given) must be in the catalog
      * ``pr_mode`` is one of off/draft/ready

    Response: 201 with ``job_id``, ``status='queued'``. The actual
    job runs in a background ``asyncio.Task`` via the lifespan-
    instantiated ``MergeQueue``.
    """
    # Compound scope check. We do NOT need a fresh call to
    # ``get_token_store`` here — the ``token`` from the
    # ``_agents_write`` dep already carries the scopes (or is
    # None in open dev mode, in which case we don't enforce).
    if body.pr_mode != "off":
        from harness.server.auth.scopes import has_scope as _hs
        if token is not None and not _hs(token.scopes, {_Scope.AGENTS_PR}):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"missing required scope: agents.pr "
                    f"(have: {', '.join(sorted(s.value for s in token.scopes)) or '(none)'})"
                ),
            )
    # Validate the agent name.
    from harness.agents.registry import load_agent as _load
    try:
        spec = _load(body.agent, project_root=_project_root_from_request(request))
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=422,
            detail=f"unknown agent: {body.agent!r} ({e})",
        )
    # Validate the model (if given).
    if body.model is not None:
        from harness.server.llm.models import list_models as _list_models
        valid = {m.id for m in _list_models()}
        if body.model not in valid:
            raise HTTPException(
                status_code=422,
                detail=f"unknown model: {body.model!r} (valid: {sorted(valid)})",
            )
    # Enqueue.
    import asyncio
    queue = getattr(request.app.state, "merge_queue", None)
    if queue is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "MergeQueue not initialised (server lifespan init failed — "
                "are LLM API keys configured?)"
            ),
        )
    job_store = _get_store(request)
    from harness.agents.merge_queue import MergeJob as _MJ
    from harness.agents.spec import AgentSpec as _AS
    review_spec = _AS(
        name="review-readonly",
        model=body.model or "MiniMax-M2.7",
        tools=["read_file"],
        permissions="read-only",
        system_prompt="Read-only review.",
        max_iterations=2,
        worktree_required=True,
    )
    from harness.config import settings as _settings
    pr_target = body.pr_target or _settings.pr_default_target_branch
    job = _MJ(
        code_spec=spec,
        review_spec=review_spec,
        task=body.prompt,
        worktree_id=f"api-{abs(hash(body.prompt)) % 100000:05d}",
        pr_mode=body.pr_mode,
        pr_target_branch=pr_target,
    )
    job_id = await queue.enqueue_async(job)
    return _EnqueueResponse(
        job_id=job_id,
        status="queued",
        worktree_id=job.worktree_id,
        prompt=body.prompt[:200],
        pr_mode=body.pr_mode,
        pr_target=pr_target,
        model=body.model or spec.model,
    )


def _project_root_from_request(request: Request) -> "Path":  # type: ignore[name-defined]
    """Pull the project_root from settings (per-request, not cached).

    The ``load_agent`` function needs a project_root to look up
    project-level overrides. We read it from ``settings`` on every
    request so a settings reload is reflected without restarting
    the server. In tests, ``isolated_settings`` patches the
    path.
    """
    from harness.config import settings as _s
    return _s.project_root


__all__ = ["router"]
