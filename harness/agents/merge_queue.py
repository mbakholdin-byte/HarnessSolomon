"""Merge queue — code agent → reviewer agent → adversarial verify → merge (Phase 2.0, Step 7 + Phase 2.1, Step 2 + Phase 2.2, Step 1).

The merge queue orchestrates a single end-to-end flow:

    1. Open a worktree branched off main.
    2. Run the **code** agent in the worktree (full perms, writes code).
    3. Run the **review** agent on the worktree (read-only, sees the diff).
    4. Run :class:`~harness.agents.verify.AdversarialVerify` over the
       review's verdict.
    5. If the panel says PASS, ``git merge --ff-only harness/<id>`` into
       the main worktree and clean up. Otherwise leave the worktree for
       human inspection.

**Serialisation (Phase 2.2):** jobs are serialised **per repo** via
:class:`~harness.agents.repo_locks.RepoLockRegistry`. Two jobs in
different repos run in parallel; two jobs in the same repo still
serialise (worktree + git operations aren't safe to run concurrently
inside one repo). The back-compat alias ``self._lock`` is a single
process-wide lock and is kept for any external caller that grabbed
the attribute directly (deprecated; use ``self._locks``).

**Timeouts:** each agent call is wrapped in :func:`asyncio.wait_for`
with ``settings.subagent_timeout_s`` (default 300s).

**Background mode (Phase 2.1):** :meth:`MergeQueue.enqueue_async` returns
a ``job_id`` immediately and runs the job in a background ``asyncio.Task``.
Status and event log are persisted via :class:`~harness.agents.jobs.JobStore`
so the CLI / Web UI can poll progress and resume after a process restart.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from harness.agents.jobs import JobEvent, JobStore
from harness.agents.outbound import OutboundWebhookDispatcher
from harness.agents.pr_integration import (
    GHUnavailable,
    add_pr_label,
    create_pr,
    enable_auto_merge,
    get_pr_status,
    merge_pr,
    wait_for_checks,
)
from harness.agents.pr_split import plan_splits
from harness.agents.repo_locks import RepoLockRegistry
from harness.agents.runner import AgentRunner
from harness.agents.spec import AgentSpec
from harness.agents.verify import AdversarialVerify
from harness.agents.worktree import WorktreeSession
from harness.config import settings
from harness.redaction import redact

logger = logging.getLogger(__name__)


#: Statuses for which ``subscribe()`` should keep streaming events.
#: Phase 2.2 extends the set with the 5 PR-phase statuses so a job
#: waiting for CI checks continues to receive events. Derived from
#: :data:`harness.agents.jobs._RUNNING_STATUSES` (private) — we
#: re-derive here to keep the import boundary clean (avoid pulling
#: a private symbol from another module).
_IN_FLIGHT_STATUSES: frozenset[str] = frozenset({
    "queued", "running_code", "running_review", "verifying",
    "pr_creating", "pr_open", "pr_waiting_checks", "pr_waiting_review", "merging_pr",
})


@dataclass
class MergeJob:
    """Inputs for a single merge-queue job."""

    code_spec: AgentSpec
    review_spec: AgentSpec
    task: str
    worktree_id: str
    model: str | None = None  # overrides the spec's model
    #: Optional cascade model overrides (Phase 2.1). When set, the
    #: queue passes the chosen tier's model to ``AgentRunner.run``
    #: via ``model_override=``. ``None`` means "use spec.model"
    #: (Phase 2.0 behaviour). Reserved for a future cascade hookup
    #: — Phase 2.1 Step 2 only stores these for the JobStore
    #: observability, it does NOT yet drive the agent loop with
    #: them (cascade is Step 1, integration is Step 4).
    model_t1: str | None = None
    model_t2: str | None = None
    model_t3: str | None = None
    # === Phase 2.2: PR integration ===
    #: ``"off"`` (default, local ff-merge), ``"draft"`` (open draft
    #: PR), or ``"ready"`` (open ready-for-review PR). See
    #: :class:`~harness.agents.pr_integration` for the full lifecycle.
    pr_mode: str = "off"
    #: Target branch the PR is opened against. ``None`` defaults to
    #: ``settings.pr_default_target_branch`` (``"main"``).
    pr_target_branch: str | None = None
    #: Optional per-job repo override. When ``None``, the queue uses
    #: ``self.runner.repo`` (Phase 2.1 single-repo behaviour). When
    #: set, the queue uses this path for the worktree + the cross-repo
    #: lock registry. This is the lever for cross-repo parallelism.
    repo_override: Path | None = None
    # === Phase 2.3: auto-merge ===
    #: When True, use ``gh pr merge --auto`` after CI checks pass
    #: (instead of merging immediately). The job transitions to
    #: ``pr_auto_merge_enabled`` and waits for an inbound webhook
    #: to mark it ``merged``. This is the recommended setting for
    #: repos with branch protection (e.g. "1 approval + green CI"
    #: rules): the merge happens as soon as all conditions clear,
    #: without the queue having to poll.
    #: When False (default), behaviour is identical to Phase 2.2:
    #: after CI is green, ``gh pr merge`` runs immediately.
    auto_merge: bool = False
    #: Override the merge method for ``enable_auto_merge``. When
    #: ``None`` (default), uses ``settings.auto_merge_method``
    #: (typically ``"squash"``).
    auto_merge_method: str | None = None
    #: Override the auto-merge label required by GitHub branch
    #: protection. When ``None`` (default), uses
    #: ``settings.auto_merge_label`` (typically
    #: ``"harness-auto-merge"``). The queue does NOT currently
    #: add this label — the operator is expected to configure
    #: branch protection to require the label, and the label is
    #: assumed to be already present on the PR. (Future Phase
    #: 2.4: auto-add the label via ``gh pr edit --add-label``.)
    auto_merge_label: str | None = None
    # === Phase 2.4: stacked / multi-PR fields ===
    #: Number of slices the job should be split into (``None`` =
    #: don't split, use the legacy single-PR path). When ``> 1``,
    #: the queue dispatches to ``_run_stack_phase`` instead of
    #: ``_run_pr_phase``. The orchestrator row (``stack_position=0``)
    #: uses the default of ``None``; child rows in the loop set
    #: this to a concrete int for observability.
    split_into: int | None = None
    #: Stack identifier (shared by all rows in the same stack).
    #: ``None`` for non-stacked jobs. The first row in a stack uses
    #: ``stack_position=0`` (the orchestrator; no PR); subsequent
    #: rows use ``stack_position >= 1`` (the actual PRs).
    stack_id: str | None = None
    #: 0-based position within the stack. ``0`` = orchestrator row
    #: (no PR); ``1..N`` = child PRs.
    stack_position: int = 0
    #: Total slice count in the stack (``1`` for non-stacked jobs).
    stack_size: int = 1
    #: For stacked jobs: the ``pr_number`` of the previous slice
    #: (``None`` for slice 0). The PR for slice N+1 is opened
    #: against slice N's branch.
    depends_on_pr_number: int | None = None
    #: Explicit list of files for this slice (overrides the
    #: planner's ``auto``/``directory`` grouping). When set, the
    #: planner only groups the listed files; unlisted files are
    #: ignored. ``None`` = use the planner on the full diff.
    slice_files: list[str] | None = None
    # === Phase 2.5: cross-repo stacks ===
    #: One repo path per slice, for cross-repo stacks. ``None``
    #: (default) = single-repo stack (Phase 2.4 behavior — all
    #: slices use ``repo_override`` or ``self.runner.repo``).
    #: When set, validation in :meth:`MergeQueue.enqueue`
    #: (and CLI/API entry points) requires
    #: ``len(stack_repos) == split_into`` AND every path to
    #: exist on disk AND be a git repo. Each slice gets its own
    #: ``WorktreeSession`` (1 worktree per repo); per-repo
    #: ``RepoLockRegistry`` lock acquired sequentially.
    stack_repos: list[Path] | None = None


@dataclass
class MergeResult:
    """Outcome of a single merge-queue job."""

    merged: bool
    reason: str
    worktree_preserved: bool
    code_iterations: int = 0
    review_iterations: int = 0
    cost: float = 0.0
    error: str | None = None
    timeout: bool = False
    # === Phase 2.2: PR integration ===
    #: PR URL (e.g. ``https://github.com/owner/repo/pull/12``). Set
    #: when ``pr_mode != "off"`` and the PR was opened. ``None`` for
    #: local-only merges.
    pr_url: str | None = None
    #: PR number extracted from the URL. ``None`` if no PR yet.
    pr_number: int | None = None
    #: True when ``pr_mode != "off"`` but ``gh`` was unavailable and
    #: the queue fell back to a local ff-merge (``pr_strategy="auto"``
    #: only). Lets callers tell apart "merged via PR" from "merged
    #: locally after PR was requested but skipped".
    pr_skipped: bool = False


class MergeQueue:
    """Run code → review → verify → merge jobs.

    Args:
        runner:   The :class:`AgentRunner` shared with the rest of the harness.
        verifier: The :class:`AdversarialVerify` panel.
        store:    Optional :class:`JobStore` for background-mode persistence
                  (Phase 2.1). When ``None``, the queue still works
                  synchronously via :meth:`enqueue` but
                  :meth:`enqueue_async` raises — background mode is opt-in.
    """

    def __init__(
        self,
        runner: AgentRunner,
        verifier: AdversarialVerify,
        *,
        store: JobStore | None = None,
        outbound: OutboundWebhookDispatcher | None = None,
    ) -> None:
        self.runner = runner
        self.verifier = verifier
        self.store = store
        # Phase 2.5: optional outbound webhook dispatcher. When
        # set, every event passed to :meth:`_emit` is also routed
        # through ``outbound.fire(...)`` (fire-and-forget). The
        # dispatcher is a singleton owned by the FastAPI lifespan
        # or the CLI dispatcher; the queue never ``await``s the
        # delivery (so a slow / down receiver cannot stall a
        # job's lifecycle).
        self._outbound = outbound
        # Phase 2.2: per-repo lock registry. Two jobs in different
        # repos can now run in parallel; same-repo jobs still serialise.
        # ``self._lock`` is kept as a back-compat alias (a process-wide
        # lock) for any external caller that grabbed the attribute
        # directly. New code should use ``self._locks.lock_for(repo)``.
        self._locks = RepoLockRegistry()
        # Back-compat: a single global lock equivalent to Phase 2.0/2.1
        # behaviour. Used by ``enqueue()`` (which doesn't yet know about
        # per-repo targeting) and any code that does ``self._lock``
        # directly. We construct it once per MergeQueue so ``acquire``
        # / ``release`` pair correctly.
        self._lock = asyncio.Lock()
        # In-process event queues for ``subscribe()`` (Phase 2.1).
        # Keyed by job_id. We use a plain dict (not the JobStore) so
        # late subscribers don't have to hit SQLite; the store is
        # the durable copy, this is the live broadcast channel.
        self._live: dict[str, asyncio.Queue[JobEvent]] = {}

    async def enqueue(self, job: MergeJob) -> MergeResult:
        """Run one job end-to-end. Jobs are serialised per-repo (Phase 2.2).

        Phase 2.2: replaced the single global ``asyncio.Lock`` with
        a per-repo registry. Two jobs in different repos can run in
        parallel; two jobs in the same repo still serialise. For
        backward compat, the sync path uses the runner's repo (Phase
        2.1 behaviour) and a new ``MergeJob.repo_override`` (Phase 2.2
        Step 3) lets cross-repo callers target a different path.

        Always returns a :class:`MergeResult` (never raises). On any
        internal failure, ``merged=False`` and ``error`` is populated.
        """
        repo = self._job_repo(job)
        async with self._locks.lock_for(repo):
            return await self._run_job(job)

    def _job_repo(self, job: MergeJob) -> Path:
        """Resolve the repo a job targets.

        Phase 2.2: prefers ``job.repo_override`` (per-job override,
        enables cross-repo parallelism via the lock registry) and
        falls back to ``self.runner.repo`` (Phase 2.1 single-repo
        behaviour).
        """
        return job.repo_override or self.runner.repo

    # === Background mode (Phase 2.1) ===

    async def enqueue_async(self, job: MergeJob) -> str:
        """Enqueue a job to run in the background; return its ``job_id``.

        The job runs in an ``asyncio.Task`` scheduled on the current
        event loop. Use :meth:`get_status` to poll or :meth:`subscribe`
        to stream events.

        The serialisation contract is preserved: at most ONE background
        job runs at a time (the same ``asyncio.Lock`` that guards
        :meth:`enqueue` also guards the background task).

        Requires a ``store`` to have been provided at construction.
        Raises ``RuntimeError`` otherwise.
        """
        if self.store is None:
            raise RuntimeError(
                "enqueue_async requires a JobStore; pass store=... to MergeQueue"
            )
        # Phase 4.1 Step 6.7: emit enqueue event.
        try:
            from harness.observability import emit_merge_queue_event
            emit_merge_queue_event(
                kind="enqueue",
                status="ok",
            )
        except Exception:  # noqa: BLE001
            pass
        job_id = await self.store.create(
            worktree_id=job.worktree_id,
            model=job.model or job.code_spec.model,
            # Phase 3: redact before persisting. The prompt is also
            # returned by ``GET /api/v1/agents/jobs/{id}`` and shown
            # in operator dashboards — redacting keeps secrets out
            # of the DB and the API surface.
            prompt=redact(job.task[:500]),   # truncate for DB display
            status="queued",
            repo=str(self._job_repo(job)),
            pr_mode=job.pr_mode,
            target_branch=job.pr_target_branch or settings.pr_default_target_branch,
        )
        # Live event queue (consumed by ``subscribe``).
        self._live[job_id] = asyncio.Queue()
        # Schedule the background runner.
        asyncio.create_task(self._run_job_async(job, job_id))
        return job_id

    async def get_status(self, job_id: str) -> str | None:
        """Return the current job status (string) or ``None`` if unknown.

        Reads from the persistent :class:`JobStore`. The result is
        up-to-date as of the last status update (or
        ``recover_running`` if the process restarted).
        """
        if self.store is None:
            return None
        rec = await self.store.load(job_id)
        return rec.status if rec is not None else None

    async def subscribe(self, job_id: str) -> AsyncIterator[JobEvent]:
        """Stream live events for a job.

        Yields the job's historical events first (replay from the
        store), then any new events as they arrive on the in-process
        queue. Terminates when the job reaches a terminal status
        (merged/failed/timeout/cancelled).
        """
        if self.store is None:
            return
        # 1. Replay historical events (so a UI connecting mid-job
        #    still sees the code_done / review_done / etc).
        async for ev in self._replay_then_live(job_id):
            yield ev

    async def _replay_then_live(self, job_id: str) -> AsyncIterator[JobEvent]:
        """Replay JobStore events, then forward from the live queue."""
        if self.store is None:
            return
        replayed = await self.store.list_events(job_id)
        for ev in replayed:
            yield ev
        # Check terminal status before consuming live (avoid blocking
        # on a queue that will never see a sentinel).
        rec = await self.store.load(job_id)
        if rec is None or rec.status not in _IN_FLIGHT_STATUSES:
            return
        queue = self._live.get(job_id)
        if queue is None:
            return
        # Consume live events until the job goes terminal. We poll
        # the store on each iteration to detect terminal state without
        # a separate "done" sentinel in the queue itself.
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Re-check terminal status periodically.
                rec = await self.store.load(job_id)
                if rec is None or rec.status not in _IN_FLIGHT_STATUSES:
                    return
                continue
            yield ev
            # The runner's last event is always ``merged``/``failed``/
            # ``timeout``/``cancelled`` — but we don't rely on a
            # sentinel kind here, we just keep consuming until the
            # store says the job is terminal. A small sleep keeps
            # tests fast without busy-spinning.

    async def _emit(self, job_id: str, kind: str, **payload: Any) -> None:
        """Append an event to the store and broadcast on the live queue."""
        if self.store is None:
            return
        await self.store.append_event(job_id, kind, payload)
        ev = JobEvent(
            id=0,  # filled by DB AUTOINCREMENT; not surfaced to callers
            job_id=job_id,
            ts="",
            kind=kind,
            payload=payload,
        )
        q = self._live.get(job_id)
        if q is not None:
            await q.put(ev)
        # Phase 2.5: fire outbound webhook for high-signal events.
        # The dispatcher filters by ``kind`` itself; we just hand
        # it the dict and let it decide. ``fire`` returns
        # immediately (fire-and-forget), so this never blocks the
        # job lifecycle.
        if self._outbound is not None:
            self._outbound.fire(
                {
                    "event": "job_event",
                    "job_id": job_id,
                    "kind": kind,
                    **payload,
                },
            )

    async def _run_job_async(self, job: MergeJob, job_id: str) -> None:
        """Background-task body. Mirrors :meth:`_run_job` but emits events.

        Note: deliberately duplicates :meth:`_run_job` to avoid
        complicating the Phase 2.0 sync path with optional callbacks.
        The two methods MUST stay in lock-step — the canonical flow
        is in :meth:`_run_job`; this one copies it line-for-line and
        adds ``await self._emit(...)`` hooks. The ``_run_job`` tests
        (Phase 2.0) cover the logic; :meth:`_run_job_async` adds
        status persistence on top.
        """
        # Phase 4.1 Step 6.7: emit start; final status emitted at end.
        try:
            from harness.observability import emit_merge_queue_event
            emit_merge_queue_event(kind="start", status="ok", job_id=job_id)
        except Exception:  # noqa: BLE001
            pass
        _final_status = "merged"
        try:
            await self._run_job_async_impl(job, job_id)
        except Exception as exc:  # noqa: BLE001
            _final_status = "failed"
            try:
                from harness.observability import emit_merge_queue_event
                emit_merge_queue_event(
                    kind="finish", status="error", job_id=job_id, error=str(exc),
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        else:
            try:
                from harness.observability import emit_merge_queue_event
                emit_merge_queue_event(
                    kind="finish", status="ok", job_id=job_id,
                )
            except Exception:  # noqa: BLE001
                pass

    async def _run_job_async_impl(self, job: MergeJob, job_id: str) -> None:
        repo = self._job_repo(job)
        async with self._locks.lock_for(repo):
            await self._emit(job_id, "started")
            try:
                # 1. Worktree.
                try:
                    worktree_ctx = WorktreeSession(
                        self.runner.repo, worktree_id=job.worktree_id,
                    )
                    wt = await worktree_ctx.__aenter__()
                except Exception as e:
                    await self.store.update_status(
                        job_id, "failed", finished=True,
                        error=f"worktree creation failed: {type(e).__name__}: {e}",
                    )
                    await self._emit(job_id, "failed", reason="worktree creation failed")
                    return

                # 2. Code agent.
                await self.store.update_status(job_id, "running_code")
                await self._emit(job_id, "running_code")
                code_result = await self._call_with_timeout(
                    self.runner.run(
                        job.code_spec, job.task,
                        worktree_id=job.worktree_id,
                        external_worktree=wt,
                    ),
                )
                if code_result.error is not None and not code_result.final_text:
                    try:
                        await worktree_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    try:
                        await worktree_ctx.delete_branch()
                    except Exception:
                        pass
                    await self.store.update_status(
                        job_id, "failed", finished=True,
                        cost=code_result.total_cost, error=code_result.error,
                    )
                    await self._emit(job_id, "failed", reason="code agent failed")
                    return
                await self._emit(job_id, "code_done", iterations=code_result.iterations)

                # 3. Review agent.
                await self.store.update_status(job_id, "running_review")
                await self._emit(job_id, "running_review")
                review_prompt = (
                    f"Original task:\n{job.task}\n\n"
                    f"Code agent's changes (review with severity scale BLOCKER/MAJOR/MINOR/NIT):\n"
                    f"{code_result.final_text}"
                )
                review_result = await self._call_with_timeout(
                    self.runner.run(
                        job.review_spec, review_prompt,
                        worktree_id=job.worktree_id,
                        external_worktree=wt,
                    ),
                )
                if review_result.error is not None and not review_result.final_text:
                    try:
                        await worktree_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    try:
                        await worktree_ctx.delete_branch()
                    except Exception:
                        pass
                    await self.store.update_status(
                        job_id, "failed", finished=True,
                        cost=code_result.total_cost + review_result.total_cost,
                        error=review_result.error,
                    )
                    await self._emit(job_id, "failed", reason="review agent failed")
                    return
                await self._emit(job_id, "review_done", iterations=review_result.iterations)

                # 4. Adversarial verify.
                await self.store.update_status(job_id, "verifying")
                await self._emit(job_id, "verifying")
                passed = await self.verifier.run(
                    prompt=job.task, answer=review_result.final_text,
                    model=job.model or job.code_spec.model,
                )

                if not passed:
                    # PRESERVE worktree for human review.
                    await self.store.update_status(
                        job_id, "failed", finished=True,
                        cost=code_result.total_cost + review_result.total_cost,
                        error="adversarial verify rejected the review",
                    )
                    await self._emit(job_id, "failed", reason="verify rejected")
                    return

                # 5. PR phase OR local ff-merge.
                # Phase 2.2: branch on ``pr_mode``. Default = local
                # ff-merge (Phase 2.1). With ``pr_mode in ("draft",
                # "ready")``, run the full PR lifecycle via
                # ``_run_pr_phase`` (open PR, wait for checks, merge).
                # Phase 2.4: when ``split_into > 1``, route to
                # ``_run_stack_phase`` (multi-PR stacked flow).
                repo = self._job_repo(job)
                if job.split_into and job.split_into > 1 and job.pr_mode != "off":
                    stack_result = await self._run_stack_phase(
                        job, job_id, repo, wt.branch,
                        cost_so_far=code_result.total_cost + review_result.total_cost,
                    )
                    if stack_result is None:
                        return
                    # stack_result shape: (merged, pr_url, pr_number, pr_skipped).
                    # For stacks, the orchestrator's pr_url is None;
                    # the caller's flow treats this as "no local
                    # cleanup needed" — the stack is now in flight
                    # and will be resolved by webhooks (Step 3).
                    return
                if job.pr_mode in ("draft", "ready"):
                    pr_result = await self._run_pr_phase(
                        job, job_id, repo, wt.branch,
                        cost_so_far=code_result.total_cost + review_result.total_cost,
                    )
                    if pr_result is None:
                        # PR phase did its own store + emit + return;
                        # do NOT continue to the local-ff-merge path.
                        return
                    # ``pr_result`` is a (merged, pr_url, pr_number, pr_skipped) tuple
                    pr_merged, pr_url, pr_number, pr_skipped = pr_result
                    if not pr_merged:
                        # PR phase already updated the store + emitted the
                        # appropriate "failed" / "pr_skipped" event.
                        return
                    # PR merged successfully (via gh pr merge).
                    try:
                        await worktree_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    try:
                        await worktree_ctx.delete_branch()
                    except Exception:
                        pass
                    await self.store.update_status(
                        job_id, "merged", finished=True,
                        cost=code_result.total_cost + review_result.total_cost,
                        pr_url=pr_url, pr_number=pr_number,
                        result_text=code_result.final_text,
                    )
                    await self._emit(
                        job_id, "merged",
                        pr_url=pr_url, pr_number=pr_number,
                    )
                    return

                # 5b. Local fast-forward merge (Phase 2.1 path, pr_mode="off").
                try:
                    await self._ff_merge(repo, wt.branch)
                except Exception as e:
                    await self.store.update_status(
                        job_id, "failed", finished=True,
                        cost=code_result.total_cost + review_result.total_cost,
                        error=f"git merge --ff-only failed: {e}",
                    )
                    await self._emit(job_id, "failed", reason="merge failed")
                    return

                # 6. Cleanup.
                try:
                    await worktree_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                try:
                    await worktree_ctx.delete_branch()
                except Exception:
                    pass
                await self.store.update_status(
                    job_id, "merged", finished=True,
                    cost=code_result.total_cost + review_result.total_cost,
                    result_text=code_result.final_text,
                )
                await self._emit(job_id, "merged")
            except _Timeout:
                await self.store.update_status(
                    job_id, "timeout", finished=True,
                    error="subagent_timeout_s exceeded",
                )
                await self._emit(job_id, "timeout")
            except Exception as e:
                logger.exception("background merge job %s crashed", job_id)
                await self.store.update_status(
                    job_id, "failed", finished=True,
                    error=f"{type(e).__name__}: {e}",
                )
                await self._emit(job_id, "failed", reason="crashed")
            finally:
                # Best-effort cleanup of the live queue. The store
                # keeps the durable record.
                self._live.pop(job_id, None)

    async def _run_job(self, job: MergeJob) -> MergeResult:
        # Phase 2.2: PR mode requires the async background path (so
        # we can ``await`` PR lifecycle events, poll CI, etc.). The
        # sync path is for fast local-ff-merge only; with PR mode it
        # returns a clear "use --background" error.
        if job.pr_mode != "off":
            return MergeResult(
                merged=False, reason="pr mode requires --background",
                worktree_preserved=False,
                error=(
                    f"pr_mode={job.pr_mode!r} is only supported via "
                    "MergeQueue.enqueue_async; use the CLI --background flag"
                ),
            )
        # Phase 2.4: stacked PRs also require the async path
        # (the stack orchestrator awaits per-slice ``create_pr``,
        # which can't be done in a sync flow).
        if job.split_into and job.split_into > 1:
            return MergeResult(
                merged=False, reason="stack mode requires --background",
                worktree_preserved=False,
                error=(
                    f"split_into={job.split_into} is only supported via "
                    "MergeQueue.enqueue_async; use the CLI --background flag"
                ),
            )

        # 1. Open the worktree.
        try:
            worktree_ctx = WorktreeSession(self.runner.repo, worktree_id=job.worktree_id)
            wt = await worktree_ctx.__aenter__()
        except Exception as e:
            return MergeResult(
                merged=False, reason="worktree creation failed",
                worktree_preserved=False,
                error=f"{type(e).__name__}: {e}",
            )

        try:
            # 2. Run the code agent.
            code_result = await self._call_with_timeout(
                self.runner.run(
                    job.code_spec, job.task,
                    worktree_id=job.worktree_id,
                    external_worktree=wt,
                ),
            )
            if code_result.error is not None and not code_result.final_text:
                # Code agent failed before producing any text. The worktree
                # is broken (no useful artifacts) — clean it up. The branch
                # is deleted too.
                try:
                    await worktree_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                try:
                    await worktree_ctx.delete_branch()
                except Exception:
                    pass
                return MergeResult(
                    merged=False, reason="code agent failed",
                    worktree_preserved=False,
                    code_iterations=code_result.iterations,
                    cost=code_result.total_cost,
                    error=code_result.error,
                )

            # 3. Run the review agent (read-only, same worktree).
            review_prompt = (
                f"Original task:\n{job.task}\n\n"
                f"Code agent's changes (review with severity scale BLOCKER/MAJOR/MINOR/NIT):\n"
                f"{code_result.final_text}"
            )
            review_result = await self._call_with_timeout(
                self.runner.run(
                    job.review_spec, review_prompt,
                    worktree_id=job.worktree_id,
                    external_worktree=wt,
                ),
            )
            if review_result.error is not None and not review_result.final_text:
                # Reviewer errored before producing output. Clean up:
                # the code branch is incomplete (no review verdict to
                # act on) and there is nothing to preserve.
                try:
                    await worktree_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                try:
                    await worktree_ctx.delete_branch()
                except Exception:
                    pass
                return MergeResult(
                    merged=False, reason="review agent failed",
                    worktree_preserved=False,
                    code_iterations=code_result.iterations,
                    review_iterations=review_result.iterations,
                    cost=code_result.total_cost + review_result.total_cost,
                    error=review_result.error,
                )

            # 4. Adversarial verify the review's verdict.
            passed = await self.verifier.run(
                prompt=job.task, answer=review_result.final_text,
                model=job.model or job.code_spec.model,
            )

            if not passed:
                # PRESERVE the worktree for human inspection. We do NOT
                # enter the finally cleanup block on this path.
                return MergeResult(
                    merged=False, reason="adversarial verify rejected the review",
                    worktree_preserved=True,
                    code_iterations=code_result.iterations,
                    review_iterations=review_result.iterations,
                    cost=code_result.total_cost + review_result.total_cost,
                )

            # 5. Fast-forward merge.
            try:
                await self._ff_merge(self.runner.repo, wt.branch)
            except Exception as e:
                # PRESERVE the worktree so the human can investigate the
                # merge conflict.
                return MergeResult(
                    merged=False, reason="git merge --ff-only failed",
                    worktree_preserved=True,
                    code_iterations=code_result.iterations,
                    review_iterations=review_result.iterations,
                    cost=code_result.total_cost + review_result.total_cost,
                    error=f"{type(e).__name__}: {e}",
                )

            # 6. After a successful merge, the branch is no longer needed.
            # Clean up the worktree + delete the branch. Return BEFORE
            # the finally block (which would do a redundant cleanup).
            try:
                await worktree_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("worktree cleanup after merge failed: %s", e)
            try:
                await worktree_ctx.delete_branch()
            except Exception as e:
                logger.warning("branch deletion after merge failed: %s", e)
            return MergeResult(
                merged=True, reason="merged",
                worktree_preserved=False,
                code_iterations=code_result.iterations,
                review_iterations=review_result.iterations,
                cost=code_result.total_cost + review_result.total_cost,
            )
        except _Timeout:
            # PRESERVE the worktree on timeout — the human can resume.
            return MergeResult(
                merged=False, reason="timeout", worktree_preserved=True,
                timeout=True,
            )
        finally:
            # On FAILURE paths (early returns above), the worktree stays
            # alive for inspection. The successful path returned BEFORE
            # this finally. We only reach here for the failure paths
            # when the worktree itself failed to be created (in which
            # case ``worktree_ctx`` was never entered and ``__aexit__``
            # is a no-op).
            pass  # explicit no-op for clarity

    async def _call_with_timeout(self, coro):
        """Wrap an awaitable in :func:`asyncio.wait_for` with the configured timeout.

        Translates :class:`asyncio.TimeoutError` to our internal
        :class:`_Timeout` sentinel so the caller's ``except`` block can
        catch timeouts without also matching cancellation events (which
        inherit from TimeoutError in Python 3.11+).
        """
        try:
            return await asyncio.wait_for(coro, timeout=settings.subagent_timeout_s)
        except (asyncio.TimeoutError, _Timeout):
            raise _Timeout() from None

    async def _run_pr_phase(
        self,
        job: MergeJob,
        job_id: str,
        repo: Path,
        head_branch: str,
        *,
        cost_so_far: float,
    ) -> tuple[bool, str | None, int | None, bool] | None:
        """Run the GitHub PR lifecycle for a job with ``pr_mode != "off"``.

        Lifecycle (Phase 2.2):
            pr_creating -> create_pr() -> pr_open
            -> pr_waiting_checks -> wait_for_checks()
            -> (pr_waiting_review if review_required)
            -> merging_pr -> merge_pr() -> merged

        On ``GHUnavailable`` (gh missing or not authenticated):
          - ``pr_strategy="auto"``: emit ``pr_skipped`` event, log a
            warning, and fall back to a local ``_ff_merge``. The
            caller's branch in ``_run_job_async`` continues with the
            normal local-merge cleanup. We return ``(True, None,
            None, True)`` where ``pr_skipped=True`` signals the
            caller to update status as ``merged`` (not ``pr_merged``).
          - ``pr_strategy="strict"``: emit ``pr_failed`` event, mark
            the job ``failed`` with the ``GHUnavailable`` message,
            preserve the worktree for human inspection. Return
            ``(False, None, None, False)``.

        On any other failure (PR create fails, checks timeout, merge
        is rejected): mark the job ``failed`` with a descriptive
        error, preserve the worktree, and return ``(False, ...)``.

        Returns:
            ``None`` if the PR phase is not applicable (i.e. an
            earlier failure short-circuited the lifecycle — currently
            not used, but reserved for future expansion).
            Otherwise a 4-tuple ``(merged, pr_url, pr_number,
            pr_skipped)``. ``pr_skipped=True`` means the queue fell
            back to a local merge (auto-strategy).
        """
        target_branch = job.pr_target_branch or settings.pr_default_target_branch
        # Phase 3: redact PII / secrets from the PR title and the
        # task text that flows into the body template. The title is
        # published to GitHub and visible to anyone with repo
        # access (often public). The body redaction happens later,
        # inside the render call.
        title = redact(f"harness: {job.task[:80]}")  # truncate to 80 chars for the title
        # Phase 2.4: render the body via the templating layer instead
        # of the inline f-string used in Phase 2.2/2.3. Extracts issue
        # numbers from the task text, supports stack metadata, and
        # falls back to the default template if ``settings.pr_template_path``
        # is empty.
        from harness.agents.pr_templating import (
            extract_issue_numbers,
            parse_codeowners_for_diff,
            render_pr_body,
        )
        issues = extract_issue_numbers(
            job.task, settings.pr_issue_link_re,
        )
        template_path = (
            Path(settings.pr_template_path)
            if settings.pr_template_path else None
        )
        # Phase 2.5: pull CODEOWNERS reviewers. Best-effort — if
        # the file is missing or malformed, we get ``[]`` and the
        # body just renders the "no reviewers" placeholder.
        diff_files_for_owners = await self._get_diff_files(
            repo, target_branch,
        )
        codeowners_reviewers = parse_codeowners_for_diff(
            repo, diff_files_for_owners,
        )
        body = render_pr_body(
            task=job.task,
            head_branch=head_branch,
            base_branch=target_branch,
            template_path=template_path,
            slice_index=(
                (job.stack_position - 1)
                if job.stack_position >= 1 else None
            ),
            slice_total=(job.stack_size if job.stack_size > 1 else None),
            stack_id=job.stack_id,
            issue_numbers=issues,
            codeowners_reviewers=codeowners_reviewers,
            test_summary="Run the test suite and verify the new tests pass.",
        )
        # Phase 3: redact the rendered PR body. The body is published
        # to GitHub and visible to anyone with repo access; we
        # already redacted the title above and now ensure the body
        # is also clean.
        body = redact(body)

        # === 1. Create the PR ===
        try:
            await self.store.update_status(job_id, "pr_creating")
            await self._emit(job_id, "pr_creating", target=target_branch)
            created = await create_pr(
                repo=repo, head_branch=head_branch, base_branch=target_branch,
                title=title, body=body, draft=(job.pr_mode == "draft"),
                env_var=settings.github_token_env,
            )
        except GHUnavailable as e:
            if settings.pr_strategy == "auto":
                logger.warning(
                    "gh unavailable for job %s, falling back to local merge: %s "
                    "(hint: %s)", job_id, e, e.hint,
                )
                await self._emit(
                    job_id, "pr_skipped",
                    reason="gh unavailable", hint=e.hint,
                )
                # Local fallback: do the ff-merge right here. The
                # caller in ``_run_job_async`` will see the tuple and
                # NOT proceed to the local-ff-merge again (we return
                # ``merged=True`` with ``pr_skipped=True``).
                try:
                    await self._ff_merge(repo, head_branch)
                except Exception as merge_err:
                    await self.store.update_status(
                        job_id, "failed", finished=True,
                        cost=cost_so_far,
                        error=(
                            f"gh unavailable and local fallback merge failed: "
                            f"{merge_err}"
                        ),
                    )
                    await self._emit(
                        job_id, "failed", reason="local fallback merge failed",
                    )
                    return (False, None, None, True)
                return (True, None, None, True)
            # strict: PR is required; treat as failure.
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far, error=f"gh unavailable: {e} (hint: {e.hint})",
            )
            await self._emit(job_id, "failed", reason="gh unavailable")
            return (False, None, None, False)
        except Exception as e:
            # PR create itself failed (network, auth, repo state, etc.)
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far, error=f"gh pr create failed: {e}",
            )
            await self._emit(job_id, "failed", reason="pr create failed")
            return (False, None, None, False)

        # === 1b. Auto-add label (Phase 2.5) ===
        # Best-effort: if ``gh pr edit --add-label`` fails
        # (label doesn't exist, network glitch, etc.) we log
        # and continue. The auto-merge step below will surface
        # the real branch-protection error if the missing
        # label was the only blocker.
        if job.auto_merge and settings.auto_add_label:
            label = (
                job.auto_merge_label
                or settings.auto_merge_label
            )
            try:
                await add_pr_label(
                    repo=repo, pr_number=created.number, label=label,
                    env_var=settings.github_token_env,
                )
                await self._emit(
                    job_id, "label_added", label=label,
                    pr_number=created.number,
                )
            except Exception as e:
                logger.warning(
                    "job %s: auto-add label %r failed (%s); "
                    "continuing without it",
                    job_id, label, e,
                )
                await self._emit(
                    job_id, "label_failed", label=label, error=str(e),
                )

        # PR created successfully.
        await self.store.update_status(
            job_id, "pr_open", pr_url=created.url, pr_number=created.number,
        )
        await self._emit(
            job_id, "pr_open",
            url=created.url, number=created.number,
        )

        # === 2. Wait for CI checks + review ===
        try:
            await self.store.update_status(job_id, "pr_waiting_checks")
            await self._emit(job_id, "pr_waiting_checks", pr_number=created.number)
            status = await wait_for_checks(
                repo=repo, pr_number=created.number,
                poll_s=settings.pr_poll_interval_s,
                timeout_s=settings.pr_wait_timeout_s,
                env_var=settings.github_token_env,
            )
            # Phase 2.5: signal that a human review is needed
            # BEFORE we attempt to merge. The outbound dispatcher
            # uses this to ping Slack / dashboard so an operator
            # knows to look at the PR. (``wait_for_checks``
            # already returned, so checks are green; the
            # remaining blocker is reviewer approval.)
            if status.review_decision == "review_required":
                await self.store.update_status(job_id, "pr_waiting_review")
                await self._emit(
                    job_id, "pr_waiting_review",
                    pr_url=created.url, pr_number=created.number,
                )
        except asyncio.TimeoutError:
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far,
                pr_url=created.url, pr_number=created.number,
                error=(
                    f"PR checks timed out after {settings.pr_wait_timeout_s}s"
                ),
            )
            await self._emit(
                job_id, "failed",
                reason="pr checks timeout",
                pr_url=created.url,
            )
            return (False, created.url, created.number, False)
        except Exception as e:
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far,
                pr_url=created.url, pr_number=created.number,
                error=f"PR wait failed: {e}",
            )
            await self._emit(job_id, "failed", reason="pr wait failed")
            return (False, created.url, created.number, False)

        # Inspect the final status.
        if status.review_decision == "changes_requested":
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far,
                pr_url=created.url, pr_number=created.number,
                error="PR review requested changes",
            )
            await self._emit(
                job_id, "failed", reason="review changes requested",
                pr_url=created.url,
            )
            return (False, created.url, created.number, False)
        if status.checks_state == "failure":
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far,
                pr_url=created.url, pr_number=created.number,
                error="PR CI checks failed",
            )
            await self._emit(
                job_id, "failed", reason="pr checks failed",
                pr_url=created.url,
            )
            return (False, created.url, created.number, False)
        if status.merged or status.state == "merged":
            # Someone merged it out-of-band; we're done.
            await self.store.update_status(
                job_id, "merged", finished=True,
                cost=cost_so_far,
                pr_url=created.url, pr_number=created.number,
            )
            await self._emit(
                job_id, "merged", reason="merged out-of-band",
                pr_url=created.url,
            )
            return (True, created.url, created.number, False)

        # === 3. Merge the PR ===
        # Phase 2.3: branch on ``job.auto_merge``. When True, use
        # ``gh pr merge --auto`` (branch-protection-aware) and
        # transition the job to ``pr_auto_merge_enabled`` — the
        # actual ``merged`` transition is delivered by an inbound
        # ``pull_request`` webhook (see
        # :mod:`harness.agents.webhook_handler`). When False, the
        # queue merges immediately (Phase 2.2 behaviour).
        if job.auto_merge:
            try:
                merge_method = job.auto_merge_method or settings.auto_merge_method
                await self.store.update_status(job_id, "merging_pr")
                await self._emit(job_id, "merging_pr", pr_number=created.number)
                await enable_auto_merge(
                    repo=repo, pr_number=created.number,
                    merge_method=merge_method,
                    delete_branch=settings.auto_merge_delete_branch,
                    env_var=settings.github_token_env,
                )
            except Exception as e:
                # Auto-merge could not be enabled (e.g. branch
                # protection is not configured for this branch).
                # Fall back to an immediate ``gh pr merge`` so the
                # user still gets a merge (backward compat with
                # the Phase 2.2 contract).
                logger.warning(
                    "job %s: enable_auto_merge failed (%s); "
                    "falling back to direct merge_pr",
                    job_id, e,
                )
                try:
                    await merge_pr(
                        repo=repo, pr_number=created.number,
                        squash=(merge_method == "squash"),
                        delete_branch=settings.auto_merge_delete_branch,
                        env_var=settings.github_token_env,
                    )
                except Exception as merge_err:
                    await self.store.update_status(
                        job_id, "failed", finished=True,
                        cost=cost_so_far,
                        pr_url=created.url, pr_number=created.number,
                        error=(
                            f"auto-merge failed and fallback merge failed: "
                            f"{merge_err}"
                        ),
                    )
                    await self._emit(
                        job_id, "failed",
                        reason="auto-merge and fallback merge failed",
                        pr_url=created.url,
                    )
                    return (False, created.url, created.number, False)
                # Fallback merge succeeded.
                return (True, created.url, created.number, False)
            # Auto-merge enabled — job terminated, waiting for
            # the inbound ``pull_request`` webhook. We do NOT
            # call ``update_status('merged', finished=True)``
            # here: the webhook handler does that when GitHub
            # actually performs the merge.
            await self.store.update_status(
                job_id, "pr_auto_merge_enabled",
                cost=cost_so_far,
                pr_url=created.url, pr_number=created.number,
            )
            await self._emit(
                job_id, "pr_auto_merge_enabled",
                pr_url=created.url, pr_number=created.number,
            )
            return (True, created.url, created.number, False)

        # Default (Phase 2.2 path): merge immediately after CI green.
        try:
            await self.store.update_status(job_id, "merging_pr")
            await self._emit(job_id, "merging_pr", pr_number=created.number)
            result = await merge_pr(
                repo=repo, pr_number=created.number,
                squash=(job.pr_mode == "draft"),
                delete_branch=True,
                env_var=settings.github_token_env,
            )
        except Exception as e:
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far,
                pr_url=created.url, pr_number=created.number,
                error=f"gh pr merge failed: {e}",
            )
            await self._emit(
                job_id, "failed", reason="pr merge failed",
                pr_url=created.url,
            )
            return (False, created.url, created.number, False)

        # PR merged successfully.
        return (True, created.url, created.number, False)

    # === Phase 2.4: stacked / multi-PR orchestration ===

    async def _get_diff_files(
        self,
        repo: Path,
        base_branch: str,
    ) -> list[str]:
        """List files changed in the worktree vs ``base_branch``.

        Phase 2.4: used by ``_run_stack_phase`` to feed
        :func:`harness.agents.pr_split.plan_splits`. Runs
        ``git -C <repo> diff --name-only <base>`` and parses the
        output. Returns ``[]`` if the diff is empty (no changes),
        the base branch doesn't exist (orphan worktree), or git
        fails (logged + empty list, never raises).

        Note: the worktree is checked out to a feature branch
        (``harness/<worktree_id>``) when this is called — the diff
        vs ``base_branch`` (``main`` by default) shows the agent's
        output.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo), "diff", "--name-only",
                base_branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30,
            )
        except (asyncio.TimeoutError, FileNotFoundError) as e:
            logger.warning(
                "_get_diff_files: git diff failed for repo=%s base=%s: %s",
                repo, base_branch, e,
            )
            return []
        if proc.returncode != 0:
            logger.warning(
                "_get_diff_files: git diff rc=%d: %s",
                proc.returncode,
                (stderr or b"").decode("utf-8", errors="replace").strip(),
            )
            return []
        return [
            line.strip()
            for line in (stdout or b"").decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]

    async def _commit_slice(
        self,
        repo: Path,
        branch: str,
        files: list[str],
        message: str,
    ) -> bool:
        """Checkout a new branch, commit ``files`` only, return success.

        Phase 2.4: used by ``_run_stack_phase`` to materialise each
        slice as its own branch in the same worktree. Returns
        ``True`` on success, ``False`` on any failure (logged).

        Sequence:
          1. ``git checkout -b <branch>`` (or ``-B`` if it exists)
          2. ``git reset HEAD~<N>`` if previous slice left uncommitted
             changes (we don't — each slice is atomic)
          3. ``git add <files>`` (only this slice's files)
          4. ``git commit -m <message>``
          5. ``git push -u origin <branch>`` (first push of this branch)

        We do NOT push here — the orchestrator decides per-slice
        whether to push (caller passes the result to ``create_pr``,
        which expects the branch to be pushed first).
        """
        try:
            # 1. Create/switch to the slice branch.
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo), "checkout", "-B", branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.error(
                    "_commit_slice: checkout -B %s failed: %s",
                    branch,
                    (stderr or b"").decode("utf-8", errors="replace").strip(),
                )
                return False
            # 2. Add the slice's files.
            if not files:
                logger.warning(
                    "_commit_slice: slice %s has no files to commit", branch,
                )
                return True  # nothing to commit, but branch exists
            add_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo), "add", "--", *files,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(add_proc.communicate(), timeout=30)
            if add_proc.returncode != 0:
                logger.error(
                    "_commit_slice: git add failed for %s: %s",
                    branch,
                    (stderr or b"").decode("utf-8", errors="replace").strip(),
                )
                return False
            # 3. Commit. ``--allow-empty`` is unnecessary — if there's
            # nothing to commit, ``git commit`` will fail with a
            # clear message and we treat that as success (the
            # branch still exists; the slice may be informational).
            commit_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo), "commit",
                "-m", message,
                "--no-verify",  # skip hooks (Phase 2.4: speed; operators can override)
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                commit_proc.communicate(), timeout=60,
            )
            if commit_proc.returncode != 0:
                # "nothing to commit" is a non-fatal no-op.
                err = (stderr or b"").decode("utf-8", errors="replace").strip()
                if "nothing to commit" in err:
                    logger.info(
                        "_commit_slice: %s has nothing to commit (no-op)",
                        branch,
                    )
                    return True
                logger.error(
                    "_commit_slice: git commit failed for %s: %s",
                    branch, err,
                )
                return False
            return True
        except (asyncio.TimeoutError, FileNotFoundError) as e:
            logger.error("_commit_slice: %s: %s", branch, e)
            return False

    async def _push_branch(self, repo: Path, branch: str) -> bool:
        """``git push -u origin <branch>``. Returns success bool.

        Phase 2.4: pushes each slice branch to ``origin`` before
        ``create_pr`` is called. Idempotent — ``-u`` is fine for
        subsequent pushes (just sets the upstream; harmless).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo), "push", "-u", "origin", branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                err = (stderr or b"").decode("utf-8", errors="replace").strip()
                logger.error(
                    "_push_branch: push origin %s failed: %s",
                    branch, err,
                )
                return False
            return True
        except (asyncio.TimeoutError, FileNotFoundError) as e:
            logger.error("_push_branch: %s: %s", branch, e)
            return False

    async def _run_stack_phase(
        self,
        job: MergeJob,
        job_id: str,
        repo: Path,
        worktree_branch: str,
        *,
        cost_so_far: float,
    ) -> tuple[bool, str | None, int | None, bool] | None:
        """Phase 2.4 stacked PR orchestration.

        Replaces the single-PR ``_run_pr_phase`` when ``job.split_into
        and job.split_into > 1``. Splits the worktree's diff into N
        slices, creates N branches in the SAME worktree (no extra
        worktrees — just ``git checkout -B``), opens N dependent PRs
        (PR-N+1's base = PR-N's branch), waits for each to merge, and
        promotes the orchestrator row to ``merged`` after the last
        child.

        Architecture decisions (see plan-файл replicated-sleeping-muffin.md
        Step 2):

          - **N branches in 1 worktree** (not N worktrees) — the
            existing worktree is reused; we just checkout different
            branches. ``WorktreeSession`` doesn't support mid-life
            branch switching, but the worktree IS just a checkout —
            git handles it.

          - **Per-repo ``RepoLockRegistry``** keeps the stack
            serialised inside one repo (multiple stacks in different
            repos can run in parallel via ``repo_override``).

          - **Cascade cancel on failure**: if any child slice fails
            to open PR or fails its lifecycle, we attempt
            ``gh pr close <N>`` for the previously-opened siblings to
            keep the remote tidy. The parent row goes to ``failed``.

          - **No ``local ff-merge`` for stacks**: stacks require
            GitHub (no point in 3 stacked PRs all in one local
            branch). If ``gh`` is unavailable, the parent row goes
            to ``failed`` with a clear error, regardless of
            ``pr_strategy`` (Phase 2.2 auto-fallback only makes sense
            for single-PR local merging).

        Lifecycle:

            orchestrator (stack_position=0) → pr_creating →
            split + create branches + push + create_pr per slice
            (each child: pr_creating → pr_open → ... → merged)
            → orchestrator → merged (after last child)

        Returns:
            Same shape as ``_run_pr_phase``: a 4-tuple
            ``(merged, pr_url, pr_number, pr_skipped)``. The
            orchestrator's ``pr_url`` and ``pr_number`` are ``None``
            (it has no PR); the caller can read child PRs via
            ``find_jobs_by_stack_id``.
        """
        target_branch = job.pr_target_branch or settings.pr_default_target_branch
        n_slices = job.split_into or 1

        # 1. Compute the diff and split plan.
        diff_files = await self._get_diff_files(repo, target_branch)
        if not diff_files:
            # Empty diff — fall back to single-PR legacy path (the
            # agent didn't change anything; treat as a no-op merge).
            await self.store.update_status(
                job_id, "merged", finished=True,
                cost=cost_so_far,
                error="stack with empty diff; nothing to split",
            )
            await self._emit(
                job_id, "merged",
                reason="empty diff", stack_size=0,
            )
            return (True, None, None, False)

        # Honour explicit ``slice_files`` override (CLI flag).
        files_for_plan = job.slice_files or diff_files
        plan = plan_splits(
            diff_files=files_for_plan,
            strategy=settings.pr_split_strategy,
            worktree_id=job.worktree_id,
            task=job.task,
            n_slices=n_slices,
            max_files_per_slice=settings.pr_split_max_files_per_slice,
            min_slices=settings.pr_split_min_slices,
            max_slices=settings.pr_split_max_slices,
        )
        if len(plan) <= 1:
            # Planner collapsed to a single slice — fall through to
            # the single-PR path (Phase 2.2/2.3 behaviour).
            logger.info(
                "_run_stack_phase: planner collapsed %d diff files to "
                "1 slice; using single-PR path",
                len(diff_files),
            )
            return await self._run_pr_phase(
                job, job_id, repo, worktree_branch,
                cost_so_far=cost_so_far,
            )

        # Assign the stack_id if not set (the orchestrator row was
        # created with stack_id=None; the children get the same id).
        stack_id = job.stack_id or uuid4().hex[:12]
        # Phase 2.5: cross-repo validation. If the user supplied
        # ``stack_repos``, ``len(stack_repos)`` must match the
        # planner's slice count (1 slice = 1 repo).
        is_cross_repo = bool(job.stack_repos)
        if is_cross_repo and len(job.stack_repos) != len(plan):  # type: ignore[arg-type]
            err = (
                f"cross-repo stack mismatch: stack_repos has "
                f"{len(job.stack_repos)} entries, planner produced "
                f"{len(plan)} slices"
            )
            await self.store.update_status(
                job_id, "failed", finished=True,
                cost=cost_so_far, error=err,
            )
            await self._emit(
                job_id, "failed", reason=err,
            )
            return (False, None, None, False)
        # Persist the orchestrator's stack_id + size BEFORE creating
        # children, so the child ``pr_stack_id`` foreign-key-style
        # lookup works. We bypass ``update_status`` (which only
        # touches status/cost/error/finished_at/pr_url/pr_number) and
        # do a direct UPDATE — this is the one place we need to
        # touch stack columns.
        import aiosqlite
        import json
        async with aiosqlite.connect(self.store.db_path) as db:
            await db.execute(
                """
                UPDATE merge_jobs
                SET pr_stack_id = ?, stack_size = ?, stack_repos = ?
                WHERE id = ?
                """,
                (
                    stack_id, len(plan),
                    json.dumps([str(p) for p in job.stack_repos])
                    if job.stack_repos else None,
                    job_id,
                ),
            )
            await db.commit()
        await self.store.update_status(job_id, "pr_creating")
        await self._emit(
            job_id, "pr_creating",
            target=target_branch, stack_id=stack_id,
            stack_size=len(plan),
            cross_repo=is_cross_repo,
        )

        # 2. Create each slice: branch + commit + push + open PR.
        opened_prs: list[tuple[int, int]] = []  # (position, pr_number)
        prev_pr_number: int | None = None
        for i, slice in enumerate(plan):
            # Phase 2.5: per-slice repo. For single-repo stacks
            # ``job.stack_repos is None`` and we keep using ``repo``
            # (the worktree repo). For cross-repo stacks, we use
            # ``stack_repos[i]`` — each slice lives in its own
            # repo (Phase 2.4 reused 1 worktree for N slices; that
            # only works inside one repo).
            repo_slice: Path = (
                Path(job.stack_repos[i])  # type: ignore[index]
                if is_cross_repo
                else repo
            )
            slice_branch = slice.branch_name
            base = (
                target_branch
                if i == 0
                else f"harness/{job.worktree_id}/step-{i - 1}"
            )
            # If the user passed explicit slice_files AND this slice
            # is the i-th one, the planner already grouped the right
            # files. Otherwise, fall back to the diff slice.
            slice_files = slice.files
            # Phase 3: commit message is pushed to origin and lives
            # forever in git history. Redact before committing.
            commit_msg = redact(
                f"harness: stack slice {i + 1}/{len(plan)}\n\n"
                f"Task: {job.task[:200]}"
            )
            ok = await self._commit_slice(
                repo_slice, slice_branch, slice_files, commit_msg,
            )
            if not ok:
                # Cascade cancel siblings.
                await self._emit(
                    job_id, "failed",
                    reason=f"slice {i + 1} commit failed",
                )
                return await self._cancel_stack(
                    job_id, stack_id, opened_prs, repo_slice,
                    cost_so_far=cost_so_far,
                    error=f"slice {i + 1} commit failed",
                )
            pushed = await self._push_branch(repo_slice, slice_branch)
            if not pushed:
                await self._emit(
                    job_id, "failed",
                    reason=f"slice {i + 1} push failed",
                )
                return await self._cancel_stack(
                    job_id, stack_id, opened_prs, repo_slice,
                    cost_so_far=cost_so_far,
                    error=f"slice {i + 1} push failed",
                )

            # 3. Create the PR for this slice.
            try:
                # Render the body for this slice (Phase 2.4 templating).
                from harness.agents.pr_templating import (
                    extract_issue_numbers,
                    render_pr_body,
                )
                issues = extract_issue_numbers(
                    job.task, settings.pr_issue_link_re,
                )
                template_path = (
                    Path(settings.pr_template_path)
                    if settings.pr_template_path else None
                )
                slice_body = render_pr_body(
                    task=job.task,
                    head_branch=slice_branch,
                    base_branch=base,
                    template_path=template_path,
                    slice_index=i, slice_total=len(plan),
                    stack_id=stack_id,
                    issue_numbers=issues,
                )
                created = await create_pr(
                    repo=repo_slice,
                    head_branch=slice_branch,
                    base_branch=base,
                    title=slice.title,
                    body=slice_body,
                    draft=(job.pr_mode == "draft"),
                    env_var=settings.github_token_env,
                )
                # Phase 2.5: per-slice auto-add label (best-effort).
                if job.auto_merge and settings.auto_add_label:
                    label = (
                        job.auto_merge_label
                        or settings.auto_merge_label
                    )
                    try:
                        await add_pr_label(
                            repo=repo_slice, pr_number=created.number,
                            label=label,
                            env_var=settings.github_token_env,
                        )
                    except Exception as e:
                        logger.warning(
                            "stack slice %d/%d (job %s): auto-add "
                            "label %r failed (%s); continuing",
                            i + 1, len(plan), job_id, label, e,
                        )
            except GHUnavailable as e:
                # Stacks require gh — no local fallback.
                await self.store.update_status(
                    job_id, "failed", finished=True,
                    cost=cost_so_far,
                    error=f"gh unavailable for stack: {e}",
                )
                await self._emit(
                    job_id, "failed",
                    reason="gh unavailable (stacks require gh)",
                )
                return await self._cancel_stack(
                    job_id, stack_id, opened_prs, repo_slice,
                    cost_so_far=cost_so_far,
                    error=f"gh unavailable: {e}",
                )
            except Exception as e:
                await self.store.update_status(
                    job_id, "failed", finished=True,
                    cost=cost_so_far,
                    error=f"slice {i + 1} create_pr failed: {e}",
                )
                await self._emit(
                    job_id, "failed",
                    reason=f"slice {i + 1} create_pr failed",
                )
                return await self._cancel_stack(
                    job_id, stack_id, opened_prs, repo_slice,
                    cost_so_far=cost_so_far,
                    error=str(e),
                )

            # 4. Persist a child row in merge_jobs. The orchestrator
            # row is the parent's job_id; children get fresh ids.
            # Phase 2.5: for cross-repo stacks, persist the
            # child row's ``repo`` as ``str(repo_slice)`` so the
            # webhook handler's ``find_job_by_pr_number`` returns
            # the right repo for the merge step.
            child_id = await self.store.create(
                worktree_id=f"{job.worktree_id}-step-{i}",
                model=job.model or "stack-child",
                prompt=f"slice {i + 1}/{len(plan)}: {job.task}",
                status="pr_open",
                repo=str(repo_slice),
                pr_mode=job.pr_mode,
                target_branch=base,
                pr_url=created.url,
                pr_number=created.number,
                pr_stack_id=stack_id,
                stack_position=i + 1,
                stack_size=len(plan),
                depends_on_pr_number=prev_pr_number,
            )
            await self._emit(
                child_id, "pr_open",
                pr_url=created.url, pr_number=created.number,
                parent_job_id=job_id,
            )
            opened_prs.append((i + 1, created.number))
            prev_pr_number = created.number

        # 5. Each child PR will be merged via its own
        # ``_run_pr_phase`` lifecycle (called by the dispatcher when
        # the child gets a ``pull_request.closed+merged`` webhook).
        # The orchestrator row stays in ``pr_creating`` until all
        # children are merged, then transitions to ``merged`` via
        # ``JobStore.all_stack_children_merged`` in
        # ``WebhookHandler.dispatch_event`` (Step 3).
        await self.store.update_status(
            job_id, "pr_open",
            cost=cost_so_far,
        )
        await self._emit(
            job_id, "pr_open",
            stack_id=stack_id,
            stack_size=len(plan),
            children=opened_prs,
        )
        return (False, None, None, False)  # orchestrator not yet "merged"

    async def _cancel_stack(
        self,
        orchestrator_id: str,
        stack_id: str,
        opened_prs: list[tuple[int, int]],
        repo: Path,
        *,
        cost_so_far: float,
        error: str,
    ) -> tuple[bool, str | None, int | None, bool]:
        """Cascade-cancel a partially-created stack.

        Phase 2.4: when one slice fails (commit / push / create_pr),
        we close the previously-opened child PRs via
        ``gh pr close <N>`` to keep the remote tidy, then mark the
        orchestrator row ``failed``. Returns the standard
        ``(merged, pr_url, pr_number, pr_skipped)`` tuple — ``False``
        for ``merged`` signals the caller to stop.
        """
        for pos, pr_number in opened_prs:
            try:
                from harness.agents.pr_integration import _gh, _env_for_token
                await check_gh_available(env_var=settings.github_token_env)
                cmd = [
                    "pr", "close", str(pr_number),
                    "--delete-branch",  # clean up the slice branch
                ]
                env = _env_for_token(settings.github_token_env)
                rc, _, stderr = await _gh(
                    *cmd, cwd=str(repo), env=env,
                )
                if rc != 0:
                    logger.warning(
                        "_cancel_stack: gh pr close %d rc=%d: %s",
                        pr_number, rc,
                        (stderr or b"").decode("utf-8", errors="replace").strip(),
                    )
            except Exception as e:
                logger.warning(
                    "_cancel_stack: close pr %d failed: %s", pr_number, e,
                )
        # Mark the orchestrator row failed. (Children stay in their
        # own status — ``pr_creating`` if the slice never created
        # the PR, ``pr_open`` if it did and we just closed it.)
        await self.store.update_status(
            orchestrator_id, "failed", finished=True,
            cost=cost_so_far, error=error,
        )
        await self._emit(
            orchestrator_id, "failed",
            reason="stack cascade-cancelled",
            stack_id=stack_id,
        )
        return (False, None, None, False)

    async def _ff_merge(self, repo: Path, branch: str) -> None:
        """``git merge --ff-only <branch>`` inside ``repo``.

        Raises on non-zero exit.
        """
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "git", "merge", "--ff-only", branch,
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git merge --ff-only {branch} failed: {err}")


class _Timeout(Exception):
    """Internal sentinel for :func:`asyncio.wait_for` timeouts.

    Note: we don't subclass :class:`asyncio.TimeoutError` on purpose —
    Python 3.11+ makes asyncio.TimeoutError == TimeoutError, and that
    would conflate our internal sentinel with cancellation. We use a
    distinct exception type and translate at the boundary
    (:meth:`MergeQueue._call_with_timeout`).
    """
