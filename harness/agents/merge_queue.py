"""Merge queue — code agent → reviewer agent → adversarial verify → merge (Phase 2.0, Step 7).

The merge queue orchestrates a single end-to-end flow:

    1. Open a worktree branched off main.
    2. Run the **code** agent in the worktree (full perms, writes code).
    3. Run the **review** agent on the worktree (read-only, sees the diff).
    4. Run :class:`~harness.agents.verify.AdversarialVerify` over the
       review's verdict.
    5. If the panel says PASS, ``git merge --ff-only harness/<id>`` into
       the main worktree and clean up. Otherwise leave the worktree for
       human inspection.

**Serialisation:** all jobs share a single :class:`asyncio.Lock`. A
parallel cross-repo queue is Phase 2.2.

**Timeouts:** each agent call is wrapped in :func:`asyncio.wait_for`
with ``settings.subagent_timeout_s`` (default 300s).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from harness.agents.runner import AgentRunner
from harness.agents.spec import AgentSpec
from harness.agents.verify import AdversarialVerify
from harness.agents.worktree import WorktreeSession
from harness.config import settings

logger = logging.getLogger(__name__)


@dataclass
class MergeJob:
    """Inputs for a single merge-queue job."""

    code_spec: AgentSpec
    review_spec: AgentSpec
    task: str
    worktree_id: str
    model: str | None = None  # overrides the spec's model


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
    """

    def __init__(self, runner: AgentRunner, verifier: AdversarialVerify) -> None:
        self.runner = runner
        self.verifier = verifier
        self._lock = asyncio.Lock()

    async def enqueue(self, job: MergeJob) -> MergeResult:
        """Run one job end-to-end. Jobs are serialised by an asyncio.Lock.

        Always returns a :class:`MergeResult` (never raises). On any
        internal failure, ``merged=False`` and ``error`` is populated.
        """
        async with self._lock:
            return await self._run_job(job)

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
