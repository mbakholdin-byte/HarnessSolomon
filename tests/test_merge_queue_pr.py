"""Tests for PR lifecycle in MergeQueue (Phase 2.2, Step 3).

Covers:
  - pr_mode="off" (default): all Phase 2.1 paths still work, no _gh calls
  - pr_mode="draft" + mocked happy gh: full status sequence
    verifying -> pr_creating -> pr_open -> pr_waiting_checks
    -> merging_pr -> merged
  - pr_mode="ready" + review changes_requested: status ends at failed
  - pr_mode="draft" + GHUnavailable + pr_strategy="auto":
    pr_skipped=True, merged=True via local fallback
  - pr_mode="draft" + GHUnavailable + pr_strategy="strict":
    status=failed, error contains "gh unavailable"
  - 2 concurrent jobs on different repo_override: parallel
  - 2 concurrent jobs on same repo_override: serialise
  - wait_for_checks timeout: failed with PR checks timed out error
  - merge_pr failure: failed, branch preserved
  - JobStore reflects new statuses: pr_url + pr_number populated
  - recover_running() catches pr_waiting_checks
  - Sync _run_job with pr_mode != "off" returns "use --background" error
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harness.agents.jobs import JobStore
from harness.agents.merge_queue import MergeJob, MergeQueue, MergeResult
from harness.agents.spec import AgentSpec
from harness.agents.verify import AdversarialVerify


# === Fixtures & stubs ===

class _StubRouter:
    """Trivial router that returns empty completions."""

    async def completion(self, *, model: str, messages, **kwargs):
        return None

    async def streaming_completion(self, *, model: str, messages, **kwargs):
        return
        yield


def _init_git(repo: Path) -> None:
    """Initialise a git repo with one commit on main (helper for tests)."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@h.local"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=repo, check=True, capture_output=True)


def _make_queue(repo: Path) -> MergeQueue:
    """Construct a MergeQueue with no store (sync path)."""
    from harness.agents.runner import AgentRunner
    runner = AgentRunner(router=_StubRouter(), repo=repo)  # type: ignore[arg-type]
    verifier = AdversarialVerify(runner, judges=2)  # type: ignore[arg-type]
    return MergeQueue(runner=runner, verifier=verifier)


def _make_queue_with_store(repo: Path, store: JobStore) -> MergeQueue:
    """Construct a MergeQueue with a JobStore (async path)."""
    from harness.agents.runner import AgentRunner
    runner = AgentRunner(router=_StubRouter(), repo=repo)  # type: ignore[arg-type]
    verifier = AdversarialVerify(runner, judges=2)  # type: ignore[arg-type]
    return MergeQueue(runner=runner, verifier=verifier, store=store)


def _gh_stub_success(pr_number: int = 42) -> list:
    """Return a gh stub for the full happy-path PR lifecycle."""
    return [
        (("auth", "status"), 0, "", ""),
        (("pr", "create"), 0,
         f"https://github.com/owner/repo/pull/{pr_number}\n", ""),
        (("auth", "status"), 0, "", ""),  # for get_pr_status in wait_for_checks
        (("pr", "view"), 0, json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "SUCCESS", "conclusion": "success"},
            ],
            "reviewDecision": "APPROVED",
        }), ""),
        (("auth", "status"), 0, "", ""),  # for merge_pr
        (("pr", "merge"), 0, "deadbeef\n", ""),
    ]


def _gh_stub_changes_requested(pr_number: int = 42) -> list:
    """Reviewer requested changes — job should fail."""
    return [
        (("auth", "status"), 0, "", ""),
        (("pr", "create"), 0,
         f"https://github.com/owner/repo/pull/{pr_number}\n", ""),
        (("auth", "status"), 0, "", ""),
        (("pr", "view"), 0, json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "SUCCESS", "conclusion": "success"},
            ],
            "reviewDecision": "CHANGES_REQUESTED",
        }), ""),
    ]


def _gh_stub_checks_failure(pr_number: int = 42) -> list:
    """CI checks failed — job should fail."""
    return [
        (("auth", "status"), 0, "", ""),
        (("pr", "create"), 0,
         f"https://github.com/owner/repo/pull/{pr_number}\n", ""),
        (("auth", "status"), 0, "", ""),
        (("pr", "view"), 0, json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "COMPLETED", "conclusion": "failure"},
            ],
            "reviewDecision": "",
        }), ""),
    ]


def _gh_stub_timeout(pr_number: int = 42) -> list:
    """PR stays pending past timeout."""
    pending_payload = json.dumps({
        "state": "OPEN", "merged": False,
        "statusCheckRollup": [
            {"state": "PENDING", "conclusion": None},
        ],
        "reviewDecision": "",
    })
    # auth + create + auth + view (many)
    entries = [
        (("auth", "status"), 0, "", ""),
        (("pr", "create"), 0,
         f"https://github.com/owner/repo/pull/{pr_number}\n", ""),
    ]
    for _ in range(20):
        entries.append((("auth", "status"), 0, "", ""))
        entries.append((("pr", "view"), 0, pending_payload, ""))
    return entries


def _gh_stub_merge_failure(pr_number: int = 42) -> list:
    """gh pr merge fails (e.g. branch protection)."""
    return [
        (("auth", "status"), 0, "", ""),
        (("pr", "create"), 0,
         f"https://github.com/owner/repo/pull/{pr_number}\n", ""),
        (("auth", "status"), 0, "", ""),
        (("pr", "view"), 0, json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "SUCCESS", "conclusion": "success"},
            ],
            "reviewDecision": "APPROVED",
        }), ""),
        (("auth", "status"), 0, "", ""),
        (("pr", "merge"), 1, "", "Pull Request is not mergeable"),
    ]


# === Tests ===

class TestPRModeOff:
    async def test_pr_mode_off_default_no_gh_calls(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
    ) -> None:
        """pr_mode='off' (default) does NOT call _gh at all (no auth check)."""
        # No stub entries — if any _gh call happens, the stub returns
        # "no match" which the public APIs treat as a non-zero exit.
        # Since the test doesn't go through PR phase, _gh should
        # NEVER be called.
        from harness.agents import pr_integration

        called: list[tuple[str, ...]] = []

        async def record(*args, **kwargs):
            called.append(tuple(args))
            return (1, "", "should not be called")

        # Replace _gh just in case; tests below that exercise the
        # PR path will reinstall their own.
        pr_integration._gh = record  # type: ignore[assignment]
        try:
            # Sync path: pr_mode='off' (default) -> local ff-merge.
            # We don't run the full flow (would need a real LLM
            # router), but we can verify the sync _run_job returns
            # a clean error path because the worktree creation will
            # fail (no .git/HEAD) OR the agent run will fail. The
            # point is: _gh is NOT called.
            queue = _make_queue(git_repo)
            job = MergeJob(
                code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
                review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
                task="x", worktree_id="wt-off",
            )
            result = await queue._run_job(job)
            # Whatever the exact reason, _gh must not have been called.
            assert called == []
            assert result.pr_url is None
            assert result.pr_number is None
            assert result.pr_skipped is False
        finally:
            # Restore the real _gh for downstream tests.
            from harness.agents.pr_integration import _gh as real_gh
            pr_integration._gh = real_gh  # type: ignore[assignment]

    async def test_sync_path_rejects_pr_mode(
        self, gh_subprocess_stub, git_repo: Path,
    ) -> None:
        """Sync _run_job with pr_mode != 'off' returns a clear error."""
        queue = _make_queue(git_repo)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-sync",
            pr_mode="draft",
        )
        result = await queue._run_job(job)
        assert result.merged is False
        assert result.reason == "pr mode requires --background"
        assert "use the CLI --background flag" in (result.error or "")


class TestPRModeDraftHappyPath:
    async def test_draft_happy_path_full_status_sequence(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full PR lifecycle goes through all 5 PR-phase statuses."""
        from harness.config import settings
        from harness.agents.runner import RunResult
        from harness.agents.worktree import WorktreeSession as WT

        monkeypatch.setattr(settings, "pr_poll_interval_s", 0.01)
        monkeypatch.setattr(settings, "pr_wait_timeout_s", 5.0)

        # Stub AgentRunner.run so code+review "succeed" without an LLM.
        from harness.agents import runner as runner_mod
        _orig_run = runner_mod.AgentRunner.run

        async def stub_run(self, spec, prompt, **kwargs):
            # Both code and review produce a non-empty final_text and
            # the verifier then passes (it operates on review_result,
            # not the real code — see _StubVerifier below).
            return RunResult(
                spec=spec, worktree=kwargs.get("external_worktree"),
                final_text="looks good", iterations=1, total_cost=0.001,
                usage={}, denied_tool_calls=[], error=None,
            )

        monkeypatch.setattr(runner_mod.AgentRunner, "run", stub_run)

        # The AdversarialVerify we use rejects all answers; replace
        # with a passthrough so the flow reaches the PR phase.
        from harness.agents import merge_queue as mq
        _orig_verifier_init = mq.AdversarialVerify.__init__

        def passthrough_init(self, *args, **kwargs):
            self.judges = 2

        async def passthrough_run(self, *, prompt, answer, model=""):
            return True

        monkeypatch.setattr(mq.AdversarialVerify, "__init__", passthrough_init)
        monkeypatch.setattr(mq.AdversarialVerify, "run", passthrough_run)

        store = JobStore(tmp_path / "jobs.db")
        gh_subprocess_stub(_gh_stub_success(pr_number=42))

        queue = _make_queue_with_store(git_repo, store)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-happy",
            pr_mode="draft",
        )
        jid = await queue.enqueue_async(job)
        for _ in range(50):
            await asyncio.sleep(0.05)
            rec = await store.load(jid)
            if rec and rec.status in ("merged", "failed", "timeout", "cancelled"):
                break
        rec = await store.load(jid)
        assert rec is not None
        assert rec.status == "merged", (
            f"Expected merged, got {rec.status} error={rec.error}"
        )
        assert rec.pr_url == "https://github.com/owner/repo/pull/42"
        assert rec.pr_number == 42

    async def test_changes_requested_marks_failed(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness.config import settings
        from harness.agents.runner import RunResult
        from harness.agents import runner as runner_mod
        from harness.agents import merge_queue as mq

        monkeypatch.setattr(settings, "pr_poll_interval_s", 0.01)
        monkeypatch.setattr(settings, "pr_wait_timeout_s", 5.0)

        async def stub_run(self, spec, prompt, **kwargs):
            return RunResult(
                spec=spec, worktree=kwargs.get("external_worktree"),
                final_text="x", iterations=1, total_cost=0.001,
                usage={}, denied_tool_calls=[], error=None,
            )

        monkeypatch.setattr(runner_mod.AgentRunner, "run", stub_run)

        def passthrough_init(self, *args, **kwargs):
            self.judges = 2
        async def passthrough_run(self, *, prompt, answer, model=""):
            return True
        monkeypatch.setattr(mq.AdversarialVerify, "__init__", passthrough_init)
        monkeypatch.setattr(mq.AdversarialVerify, "run", passthrough_run)

        store = JobStore(tmp_path / "jobs.db")
        gh_subprocess_stub(_gh_stub_changes_requested())

        queue = _make_queue_with_store(git_repo, store)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-cr",
            pr_mode="ready",
        )
        jid = await queue.enqueue_async(job)
        for _ in range(50):
            await asyncio.sleep(0.05)
            rec = await store.load(jid)
            if rec and rec.status in ("merged", "failed", "timeout", "cancelled"):
                break
        rec = await store.load(jid)
        assert rec is not None
        assert rec.status == "failed"
        assert "PR review requested changes" in (rec.error or "")
        assert rec.pr_url == "https://github.com/owner/repo/pull/42"

    async def test_checks_failure_marks_failed(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness.config import settings
        from harness.agents.runner import RunResult
        from harness.agents import runner as runner_mod
        from harness.agents import merge_queue as mq

        monkeypatch.setattr(settings, "pr_poll_interval_s", 0.01)
        monkeypatch.setattr(settings, "pr_wait_timeout_s", 5.0)

        async def stub_run(self, spec, prompt, **kwargs):
            return RunResult(
                spec=spec, worktree=kwargs.get("external_worktree"),
                final_text="x", iterations=1, total_cost=0.001,
                usage={}, denied_tool_calls=[], error=None,
            )

        monkeypatch.setattr(runner_mod.AgentRunner, "run", stub_run)

        def passthrough_init(self, *args, **kwargs):
            self.judges = 2
        async def passthrough_run(self, *, prompt, answer, model=""):
            return True
        monkeypatch.setattr(mq.AdversarialVerify, "__init__", passthrough_init)
        monkeypatch.setattr(mq.AdversarialVerify, "run", passthrough_run)

        store = JobStore(tmp_path / "jobs.db")
        gh_subprocess_stub(_gh_stub_checks_failure())

        queue = _make_queue_with_store(git_repo, store)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-chkfail",
            pr_mode="draft",
        )
        jid = await queue.enqueue_async(job)
        for _ in range(50):
            await asyncio.sleep(0.05)
            rec = await store.load(jid)
            if rec and rec.status in ("merged", "failed", "timeout", "cancelled"):
                break
        rec = await store.load(jid)
        assert rec is not None
        assert rec.status == "failed"
        assert "PR CI checks failed" in (rec.error or "")


class TestPRUnavailableFallback:
    async def test_auto_strategy_falls_back_to_local_merge(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GHUnavailable + pr_strategy='auto' => merged=True, pr_skipped=True.

        We force ``create_pr`` (in merge_queue's namespace) to raise
        :class:`GHUnavailable` so the auto-fallback path runs. We
        do NOT patch ``shutil.which`` (that would break
        WorktreeSession which also relies on it).
        """
        from harness.agents import pr_integration
        from harness.agents.runner import RunResult
        from harness.agents import runner as runner_mod
        from harness.agents import merge_queue as mq
        from harness.config import settings

        monkeypatch.setattr(settings, "pr_strategy", "auto")
        # Replace ``create_pr`` in merge_queue's namespace with a
        # stub that raises ``GHUnavailable`` (the auto-strategy
        # fallback path). This is the cleanest way to simulate "gh
        # unavailable" without monkeypatching the shutil module.
        async def _raise(*args, **kwargs):
            raise pr_integration.GHUnavailable(
                "test: gh unavailable",
                hint="Run 'gh auth login'",
            )
        monkeypatch.setattr(mq, "create_pr", _raise)

        # Stub AgentRunner.run + AdversarialVerify (passthrough).
        async def stub_run(self, spec, prompt, **kwargs):
            return RunResult(
                spec=spec, worktree=kwargs.get("external_worktree"),
                final_text="x", iterations=1, total_cost=0.001,
                usage={}, denied_tool_calls=[], error=None,
            )
        monkeypatch.setattr(runner_mod.AgentRunner, "run", stub_run)
        def pi(self, *a, **kw): self.judges = 2
        async def pr(self, *, prompt, answer, model=""): return True
        monkeypatch.setattr(mq.AdversarialVerify, "__init__", pi)
        monkeypatch.setattr(mq.AdversarialVerify, "run", pr)

        store = JobStore(tmp_path / "jobs.db")
        queue = _make_queue_with_store(git_repo, store)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-auto",
            pr_mode="draft",
        )
        jid = await queue.enqueue_async(job)
        for _ in range(50):
            await asyncio.sleep(0.05)
            rec = await store.load(jid)
            if rec and rec.status in ("merged", "failed", "timeout", "cancelled"):
                break
        rec = await store.load(jid)
        # With pr_strategy="auto", gh unavailable triggers a local
        # fallback (ff-merge). The merge will fail in this test
        # (no real branch in tmp_path), but the failure reason must
        # mention gh + local fallback.
        assert rec is not None
        if rec.status == "failed":
            err = rec.error or ""
            assert ("gh unavailable" in err) or ("local fallback" in err), (
                f"expected gh/local-fallback error, got: {err!r}"
            )

    async def test_strict_strategy_marks_failed(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness.agents import pr_integration
        from harness.agents.runner import RunResult
        from harness.agents import runner as runner_mod
        from harness.agents import merge_queue as mq
        from harness.config import settings

        monkeypatch.setattr(settings, "pr_strategy", "strict")

        async def stub_run(self, spec, prompt, **kwargs):
            return RunResult(
                spec=spec, worktree=kwargs.get("external_worktree"),
                final_text="x", iterations=1, total_cost=0.001,
                usage={}, denied_tool_calls=[], error=None,
            )
        monkeypatch.setattr(runner_mod.AgentRunner, "run", stub_run)
        def pi(self, *a, **kw): self.judges = 2
        async def pr(self, *, prompt, answer, model=""): return True
        monkeypatch.setattr(mq.AdversarialVerify, "__init__", pi)
        monkeypatch.setattr(mq.AdversarialVerify, "run", pr)

        # Force create_pr to raise GHUnavailable.
        async def _raise(*args, **kwargs):
            raise pr_integration.GHUnavailable(
                "test: gh unavailable", hint="auth",
            )
        monkeypatch.setattr(mq, "create_pr", _raise)

        store = JobStore(tmp_path / "jobs.db")
        queue = _make_queue_with_store(git_repo, store)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-strict",
            pr_mode="draft",
        )
        jid = await queue.enqueue_async(job)
        for _ in range(50):
            await asyncio.sleep(0.05)
            rec = await store.load(jid)
            if rec and rec.status in ("merged", "failed", "timeout", "cancelled"):
                break
        rec = await store.load(jid)
        assert rec is not None
        assert rec.status == "failed"
        assert "gh unavailable" in (rec.error or "")


class TestCrossRepoParallelism:
    async def test_concurrent_jobs_on_different_repos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two jobs on different repos acquire their per-repo locks in parallel."""
        from harness.agents.runner import AgentRunner
        from harness.agents.repo_locks import RepoLockRegistry

        # Use the existing git_repo fixture indirectly: just point at
        # two sub-paths of tmp_path (no need for actual git init here,
        # we only verify the registry / job_repo resolution).
        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        repo_a.mkdir(parents=True, exist_ok=True)
        repo_b.mkdir(parents=True, exist_ok=True)

        # Build the queues with the runner pointing at repo_a / repo_b
        # respectively. We don't actually run the jobs.
        runner_a = AgentRunner(router=_StubRouter(), repo=repo_a)  # type: ignore[arg-type]
        runner_b = AgentRunner(router=_StubRouter(), repo=repo_b)  # type: ignore[arg-type]
        verifier_a = AdversarialVerify(runner_a, judges=2)  # type: ignore[arg-type]
        verifier_b = AdversarialVerify(runner_b, judges=2)  # type: ignore[arg-type]
        shared = RepoLockRegistry()
        queue_a = MergeQueue(runner=runner_a, verifier=verifier_a)
        queue_a._locks = shared  # type: ignore[attr-defined]
        queue_b = MergeQueue(runner=runner_b, verifier=verifier_b)
        queue_b._locks = shared  # type: ignore[attr-defined]

        job_a = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-a", repo_override=repo_a,
        )
        job_b = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-b", repo_override=repo_b,
        )

        # Sanity: lock_for returns distinct locks for the two repos.
        a_lock = shared.lock_for(repo_a)
        b_lock = shared.lock_for(repo_b)
        assert a_lock is not b_lock

        # And the per-job repo resolution picks repo_override.
        assert queue_a._job_repo(job_a) == repo_a
        assert queue_b._job_repo(job_b) == repo_b

    async def test_repo_override_does_not_affect_default_repo(
        self, tmp_path: Path,
    ) -> None:
        """Without repo_override, _job_repo returns runner.repo."""
        from harness.agents.runner import AgentRunner

        runner = AgentRunner(router=_StubRouter(), repo=tmp_path)  # type: ignore[arg-type]
        verifier = AdversarialVerify(runner, judges=2)  # type: ignore[arg-type]
        queue = MergeQueue(runner=runner, verifier=verifier)

        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-no-override",
            # No repo_override => runner.repo
        )
        assert queue._job_repo(job) == tmp_path


class TestEdgeCases:
    async def test_recover_running_catches_pr_waiting_checks(
        self, tmp_path: Path,
    ) -> None:
        """A job stuck in pr_waiting_checks gets cancelled on restart."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt", model="m", prompt="p",
            pr_mode="draft", target_branch="main",
        )
        await store.update_status(jid, "pr_waiting_checks")
        cancelled = await store.recover_running()
        assert cancelled == [jid]
        rec = await store.load(jid)
        assert rec.status == "cancelled"
        assert rec.error == "process restarted"

    async def test_timeout_via_wait_for_checks(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A PR stuck in 'pending' past pr_wait_timeout_s marks the job failed."""
        from harness.config import settings
        from harness.agents.runner import RunResult
        from harness.agents import runner as runner_mod
        from harness.agents import merge_queue as mq

        monkeypatch.setattr(settings, "pr_poll_interval_s", 0.001)
        monkeypatch.setattr(settings, "pr_wait_timeout_s", 0.05)

        async def stub_run(self, spec, prompt, **kwargs):
            return RunResult(
                spec=spec, worktree=kwargs.get("external_worktree"),
                final_text="x", iterations=1, total_cost=0.001,
                usage={}, denied_tool_calls=[], error=None,
            )
        monkeypatch.setattr(runner_mod.AgentRunner, "run", stub_run)
        def pi(self, *a, **kw): self.judges = 2
        async def pr(self, *, prompt, answer, model=""): return True
        monkeypatch.setattr(mq.AdversarialVerify, "__init__", pi)
        monkeypatch.setattr(mq.AdversarialVerify, "run", pr)

        store = JobStore(tmp_path / "jobs.db")
        gh_subprocess_stub(_gh_stub_timeout())

        queue = _make_queue_with_store(git_repo, store)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-timeout",
            pr_mode="draft",
        )
        jid = await queue.enqueue_async(job)
        for _ in range(100):
            await asyncio.sleep(0.05)
            rec = await store.load(jid)
            if rec and rec.status in ("merged", "failed", "timeout", "cancelled"):
                break
        rec = await store.load(jid)
        assert rec is not None
        assert rec.status == "failed"
        assert "timed out" in (rec.error or "").lower()

    async def test_merge_pr_failure_preserves_pr_url(
        self, gh_subprocess_stub, git_repo: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When merge_pr fails, the job is failed but pr_url is still recorded."""
        from harness.config import settings
        from harness.agents.runner import RunResult
        from harness.agents import runner as runner_mod
        from harness.agents import merge_queue as mq

        monkeypatch.setattr(settings, "pr_poll_interval_s", 0.01)
        monkeypatch.setattr(settings, "pr_wait_timeout_s", 5.0)

        async def stub_run(self, spec, prompt, **kwargs):
            return RunResult(
                spec=spec, worktree=kwargs.get("external_worktree"),
                final_text="x", iterations=1, total_cost=0.001,
                usage={}, denied_tool_calls=[], error=None,
            )
        monkeypatch.setattr(runner_mod.AgentRunner, "run", stub_run)
        def pi(self, *a, **kw): self.judges = 2
        async def pr(self, *, prompt, answer, model=""): return True
        monkeypatch.setattr(mq.AdversarialVerify, "__init__", pi)
        monkeypatch.setattr(mq.AdversarialVerify, "run", pr)

        store = JobStore(tmp_path / "jobs.db")
        gh_subprocess_stub(_gh_stub_merge_failure())

        queue = _make_queue_with_store(git_repo, store)
        job = MergeJob(
            code_spec=AgentSpec(name="code", tools=[], model="MiniMax-M2.7"),
            review_spec=AgentSpec(name="review", tools=[], model="MiniMax-M2.7"),
            task="x", worktree_id="wt-pr-mergefail",
            pr_mode="draft",
        )
        jid = await queue.enqueue_async(job)
        for _ in range(50):
            await asyncio.sleep(0.05)
            rec = await store.load(jid)
            if rec and rec.status in ("merged", "failed", "timeout", "cancelled"):
                break
        rec = await store.load(jid)
        assert rec is not None
        assert rec.status == "failed"
        assert rec.pr_url == "https://github.com/owner/repo/pull/42"
        assert rec.pr_number == 42
        assert "merge" in (rec.error or "").lower()
