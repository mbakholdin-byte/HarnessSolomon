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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.agents.jobs import JobEvent, JobStore
from harness.agents.repo_locks import RepoLockRegistry
from harness.agents.runner import AgentRunner
from harness.agents.spec import AgentSpec
from harness.agents.verify import AdversarialVerify
from harness.agents.worktree import WorktreeSession
from harness.config import settings

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
    ) -> None:
        self.runner = runner
        self.verifier = verifier
        self.store = store
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
        """Resolve the repo a job targets. Phase 2.2 Step 3 will
        extend ``MergeJob`` with ``repo_override``; this helper
        isolates that change so we can wire per-repo locking now
        without changing the dataclass.
        """
        # Phase 2.1: only ``self.runner.repo`` exists. Phase 2.2 Step 3
        # will read ``job.repo_override`` first.
        return self.runner.repo

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
        job_id = await self.store.create(
            worktree_id=job.worktree_id,
            model=job.model or job.code_spec.model,
            prompt=job.task[:500],   # truncate for DB display
            status="queued",
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

                # 5. Fast-forward merge.
                try:
                    await self._ff_merge(self.runner.repo, wt.branch)
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
