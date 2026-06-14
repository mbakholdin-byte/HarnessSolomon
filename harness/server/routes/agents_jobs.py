"""HTTP API for the merge-queue job store (Phase 2.2, Step 4).

Endpoints:
  - ``GET /api/v1/agents/jobs/<job_id>`` — fetch one job's record
  - ``GET /api/v1/agents/jobs?recent=N`` — list the N most recent jobs
  - ``GET /api/v1/agents/health`` — queue health (stats for ops)

The router is mounted at ``/api/v1/agents`` in :mod:`harness.server.app`.
It reads from ``app.state.job_store`` (a :class:`JobStore` instance
set up in the FastAPI lifespan handler) and never constructs one
itself — the trust boundary from Phase 2.0 is preserved.

Failure modes:
  - 404 if the job_id is unknown
  - 503 if ``app.state.job_store`` is not configured (e.g. the
    lifespan handler failed to initialise it). The route is wired
    so this is observable; the rest of the server (sessions, chat)
    is unaffected.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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

    @classmethod
    def from_record(cls, rec: Any) -> "_JobRecordSchema":
        return cls(
            id=rec.id, worktree_id=rec.worktree_id, status=rec.status,
            started_at=rec.started_at, finished_at=rec.finished_at,
            cost=rec.cost, error=rec.error, model=rec.model, prompt=rec.prompt,
            repo=rec.repo, pr_url=rec.pr_url, pr_number=rec.pr_number,
            target_branch=rec.target_branch, pr_mode=rec.pr_mode,
        )


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


@router.get("/jobs/{job_id}", response_model=_JobRecordSchema)
async def get_job(job_id: str, request: Request) -> _JobRecordSchema:
    """Fetch one job by id. 404 if not found."""
    import asyncio
    store = _get_store(request)
    rec = await store.load(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return _JobRecordSchema.from_record(rec)


@router.get("/jobs", response_model=list[_JobRecordSchema])
async def list_jobs(
    request: Request, recent: int = 20,
) -> list[_JobRecordSchema]:
    """List the ``recent`` most recent jobs (default 20, newest first)."""
    import asyncio
    store = _get_store(request)
    if recent <= 0:
        return []
    recs = await store.list_recent(recent)
    return [_JobRecordSchema.from_record(r) for r in recs]


@router.get("/health", response_model=_QueueHealth)
async def queue_health(request: Request) -> _QueueHealth:
    """Ops health endpoint: per-repo lock stats + recent job count.

    The lock stats are read from ``app.state.merge_queue`` when
    available; the recent-job count is read from the store. The
    two are independent so a missing queue doesn't kill the
    health response.
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
