"""Tests for :meth:`harness.agents.merge_queue.MergeQueue._run_stack_phase`
(Phase 2.4 Step 2) and the related CLI flags.

The stack orchestrator is more complex than ``_run_pr_phase`` because it
spans multiple slices, branches, and PRs. To keep tests fast and
deterministic, we mock the I/O heavy methods (``_get_diff_files``,
``_commit_slice``, ``_push_branch``, ``create_pr``) and exercise the
orchestration logic directly.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.agents.jobs import JobStore
from harness.agents.merge_queue import MergeJob, MergeQueue
from harness.agents.pr_integration import PRCreateResult
from harness.agents.spec import AgentSpec
from harness.agents.verify import AdversarialVerify
from harness.server.llm.router import CompletionResult


# === Fixtures ===

@pytest.fixture
def code_spec() -> AgentSpec:
    return AgentSpec(
        name="code",
        model="MiniMax-M2.7", tools=["read_file", "write_file", "bash"],
        permissions="full", max_iterations=2,
        system_prompt="Make the smallest change.",
    )


@pytest.fixture
def review_spec() -> AgentSpec:
    return AgentSpec(
        name="review",
        model="MiniMax-M2.7", tools=["read_file", "grep"],
        permissions="read-only", max_iterations=2,
        system_prompt="Review.",
    )


def _build_queue(repo: Path, job_store: JobStore) -> MergeQueue:
    """Build a MergeQueue with all I/O mocks (no real LLM/git)."""
    # Stub runner — we never reach the agent loop in these tests.
    from harness.agents.runner import AgentRunner
    runner = AgentRunner.__new__(AgentRunner)
    runner.repo = repo
    runner.completion_calls = 0
    queue = MergeQueue(runner=runner, verifier=AdversarialVerify.__new__(AdversarialVerify))
    queue.store = job_store
    return queue


# === _run_stack_phase direct tests ===

class TestRunStackPhase:
    async def test_empty_diff_collapses_to_merged(
        self, tmp_path: Path,
    ) -> None:
        """Empty diff (no files changed) → orchestrator marked merged,
        no PR created, no slices."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-1", model="m", prompt="t",
        )
        queue = _build_queue(tmp_path, store)
        # Stub _get_diff_files to return empty list.
        queue._get_diff_files = AsyncMock(return_value=[])
        # No plan if diff is empty; we return early.
        result = await queue._run_stack_phase(
            job=MergeJob(
                code_spec=MagicMock(), review_spec=MagicMock(),
                task="t", worktree_id="wt-1", split_into=3,
            ),
            job_id=jid, repo=tmp_path, worktree_branch="harness/wt-1",
            cost_so_far=0.0,
        )
        assert result is not None
        merged, pr_url, pr_number, pr_skipped = result
        assert merged is True
        rec = await store.load(jid)
        assert rec.status == "merged"

    async def test_single_slice_collapses_to_pr_phase(
        self, tmp_path: Path,
    ) -> None:
        """When the planner produces 1 slice, the stack path
        delegates to ``_run_pr_phase`` (Phase 2.2/2.3 back-compat)."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-1", model="m", prompt="t",
        )
        queue = _build_queue(tmp_path, store)
        queue._get_diff_files = AsyncMock(
            return_value=["only.py"],  # 1 file → 1 slice
        )
        # Stub the inner _run_pr_phase to return a known tuple.
        queue._run_pr_phase = AsyncMock(
            return_value=(True, "https://x/1", 1, False),
        )
        result = await queue._run_stack_phase(
            job=MergeJob(
                code_spec=MagicMock(), review_spec=MagicMock(),
                task="t", worktree_id="wt-1", split_into=3,
            ),
            job_id=jid, repo=tmp_path, worktree_branch="harness/wt-1",
            cost_so_far=0.0,
        )
        assert result == (True, "https://x/1", 1, False)
        queue._run_pr_phase.assert_awaited_once()

    async def test_three_slices_creates_three_children(
        self, tmp_path: Path,
    ) -> None:
        """Three directories → 3 child PRs, all in merge_jobs."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-stack", model="m", prompt="refactor",
        )
        queue = _build_queue(tmp_path, store)
        # 3 directories with files.
        queue._get_diff_files = AsyncMock(return_value=[
            "src/a.py", "src/b.py",
            "tests/t1.py", "tests/t2.py",
            "docs/d.md",
        ])
        # Mock commit + push (always succeed).
        queue._commit_slice = AsyncMock(return_value=True)
        queue._push_branch = AsyncMock(return_value=True)
        # Mock create_pr to return increasing pr numbers.
        created_numbers = iter([10, 11, 12])
        async def fake_create_pr(*args, **kwargs):
            n = next(created_numbers)
            return PRCreateResult(
                url=f"https://github.com/o/r/pull/{n}",
                number=n, branch=kwargs["head_branch"],
            )
        # Patch at the import site in merge_queue.
        from harness.agents import merge_queue as mq_mod
        mq_mod.create_pr = fake_create_pr

        result = await queue._run_stack_phase(
            job=MergeJob(
                code_spec=MagicMock(), review_spec=MagicMock(),
                task="refactor", worktree_id="wt-stack", split_into=3,
                pr_mode="draft", pr_target_branch="main",
            ),
            job_id=jid, repo=tmp_path,
            worktree_branch="harness/wt-stack",
            cost_so_far=0.0,
        )
        # Orchestrator not yet "merged" (waits for children)
        assert result is not None
        merged, pr_url, pr_number, pr_skipped = result
        assert merged is False  # stack in flight
        assert pr_url is None  # orchestrator has no PR
        # Orchestrator row status: pr_open (waiting for children)
        rec = await store.load(jid)
        assert rec.status == "pr_open"
        # 3 child rows in merge_jobs.
        rows = await store.find_jobs_by_stack_id(rec.pr_stack_id)
        assert len(rows) == 4  # orchestrator + 3 children
        positions = sorted(r.stack_position for r in rows)
        assert positions == [0, 1, 2, 3]
        # Child 1 depends on no previous PR
        child1 = next(r for r in rows if r.stack_position == 1)
        assert child1.depends_on_pr_number is None
        # Child 2 depends on PR 10
        child2 = next(r for r in rows if r.stack_position == 2)
        assert child2.depends_on_pr_number == 10
        # Child 3 depends on PR 11
        child3 = next(r for r in rows if r.stack_position == 3)
        assert child3.depends_on_pr_number == 11
        # Each child has a pr_number
        for c in (child1, child2, child3):
            assert c.pr_number in (10, 11, 12)
            assert c.pr_url is not None
            assert c.status == "pr_open"

    async def test_commit_failure_triggers_cascade_cancel(
        self, tmp_path: Path,
    ) -> None:
        """If the 2nd slice's commit fails, the 1st slice's PR is
        closed and the orchestrator goes to ``failed``."""
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-fail", model="m", prompt="t",
        )
        queue = _build_queue(tmp_path, store)
        queue._get_diff_files = AsyncMock(return_value=[
            "src/a.py", "tests/t.py", "docs/d.md",
        ])
        # Commit succeeds for slice 0, fails for slice 1.
        queue._commit_slice = AsyncMock(side_effect=[True, False])
        queue._push_branch = AsyncMock(return_value=True)
        # _cancel_stack will try to close opened PRs; mock that.
        async def fake_create_pr(*args, **kwargs):
            return PRCreateResult(
                url="https://x/1", number=1, branch=kwargs["head_branch"],
            )
        from harness.agents import merge_queue as mq_mod
        mq_mod.create_pr = fake_create_pr
        # Mock _cancel_stack to return a known result and skip real gh.
        queue._cancel_stack = AsyncMock(
            return_value=(False, None, None, False),
        )
        result = await queue._run_stack_phase(
            job=MergeJob(
                code_spec=MagicMock(), review_spec=MagicMock(),
                task="t", worktree_id="wt-fail", split_into=2,
                pr_mode="draft", pr_target_branch="main",
            ),
            job_id=jid, repo=tmp_path,
            worktree_branch="harness/wt-fail",
            cost_so_far=0.0,
        )
        assert result is not None
        merged, pr_url, pr_number, pr_skipped = result
        assert merged is False
        # _cancel_stack was invoked with the orchestrator + opened PRs.
        queue._cancel_stack.assert_awaited_once()
        call_args = queue._cancel_stack.await_args
        assert call_args.args[0] == jid
        assert call_args.args[2] == [(1, 1)]  # 1 PR opened before failure

    async def test_gh_unavailable_marks_stack_failed(
        self, tmp_path: Path,
    ) -> None:
        """If gh is missing, the stack goes to ``failed`` (no
        local fallback for stacks)."""
        from harness.agents.pr_integration import GHUnavailable
        store = JobStore(tmp_path / "jobs.db")
        jid = await store.create(
            worktree_id="wt-nogh", model="m", prompt="t",
        )
        queue = _build_queue(tmp_path, store)
        queue._get_diff_files = AsyncMock(return_value=[
            "src/a.py", "tests/t.py",
        ])
        queue._commit_slice = AsyncMock(return_value=True)
        queue._push_branch = AsyncMock(return_value=True)
        async def fake_create_pr(*args, **kwargs):
            raise GHUnavailable(RuntimeError("gh not in PATH"))
        from harness.agents import merge_queue as mq_mod
        mq_mod.create_pr = fake_create_pr
        result = await queue._run_stack_phase(
            job=MergeJob(
                code_spec=MagicMock(), review_spec=MagicMock(),
                task="t", worktree_id="wt-nogh", split_into=2,
                pr_mode="draft", pr_target_branch="main",
            ),
            job_id=jid, repo=tmp_path,
            worktree_branch="harness/wt-nogh",
            cost_so_far=0.0,
        )
        assert result is not None
        merged, *_ = result
        assert merged is False
        rec = await store.load(jid)
        assert rec.status == "failed"
        assert "gh" in (rec.error or "").lower()


# === Sync path rejection ===

class TestSyncPathRejectsStack:
    """``_run_job`` (sync) must reject ``split_into > 1`` just like
    it rejects ``pr_mode != "off"``. The error is returned via
    ``MergeResult.error`` (no exception)."""

    def test_sync_run_job_rejects_split_into(
        self, tmp_path: Path,
    ) -> None:
        from harness.agents.merge_queue import MergeJob, MergeResult, MergeQueue
        from harness.agents.runner import AgentRunner
        from harness.agents.verify import AdversarialVerify
        runner = AgentRunner.__new__(AgentRunner)
        runner.repo = tmp_path
        queue = MergeQueue(
            runner=runner,
            verifier=AdversarialVerify.__new__(AdversarialVerify),
        )
        job = MergeJob(
            code_spec=MagicMock(), review_spec=MagicMock(),
            task="t", worktree_id="wt", split_into=3,
        )
        # Sync path is the only one that returns MergeResult directly.
        result = asyncio.run(queue._run_job(job))
        assert isinstance(result, MergeResult)
        assert result.merged is False
        assert "background" in (result.error or "")


# === CLI flag parsing ===

class TestCLIStackFlags:
    """The CLI parser accepts --split-into / --split-strategy /
    --stack-files / etc. and the validation rejects sync-path
    usage."""

    def test_split_into_flag_parses(self) -> None:
        """`harness agents run code 't' --split-into 3` parses."""
        from harness.cli import _build_parser
        p = _build_parser()
        ns = p.parse_args(
            ["agents", "run", "code", "t",
             "--split-into", "3",
             "--pr", "--background"],
        )
        assert ns.split_into == 3

    def test_split_strategy_flag_parses(self) -> None:
        from harness.cli import _build_parser
        p = _build_parser()
        ns = p.parse_args(
            ["agents", "run", "code", "t",
             "--split-into", "3",
             "--split-strategy", "directory",
             "--pr", "--background"],
        )
        assert ns.split_strategy == "directory"

    def test_split_strategy_choices_validated(self) -> None:
        """An invalid --split-strategy is rejected by argparse."""
        from harness.cli import _build_parser
        p = _build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(
                ["agents", "run", "code", "t",
                 "--split-into", "3",
                 "--split-strategy", "wrong",
                 "--pr", "--background"],
            )

    def test_split_into_without_background_exits_2(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        """`--split-into 3` without --background is rejected."""
        from harness.cli import _cmd_agents_run, _build_parser
        p = _build_parser()
        ns = p.parse_args(
            ["agents", "run", "code", "t",
             "--split-into", "3",
             "--pr"],
        )
        rc = _cmd_agents_run(ns)
        assert rc == 2
        captured = capsys.readouterr()
        assert "background" in captured.err

    def test_split_into_without_pr_exits_2(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        """`--split-into 3` without --pr is rejected (stacks need gh)."""
        from harness.cli import _cmd_agents_run, _build_parser
        p = _build_parser()
        ns = p.parse_args(
            ["agents", "run", "code", "t",
             "--split-into", "3",
             "--background"],
        )
        rc = _cmd_agents_run(ns)
        assert rc == 2
        captured = capsys.readouterr()
        assert "--pr" in captured.err or "stacks" in captured.err.lower()

    def test_stack_files_read_into_list(
        self, tmp_path: Path,
    ) -> None:
        """`--stack-files <path>` reads the file into slice_files."""
        f = tmp_path / "files.txt"
        f.write_text("a.py\n\nb.py\n  \nc.py\n", encoding="utf-8")
        from harness.cli import _build_parser
        p = _build_parser()
        ns = p.parse_args(
            ["agents", "run", "code", "t",
             "--stack-files", str(f),
             "--split-into", "3",
             "--pr", "--background"],
        )
        # argparse opens the file (FileType 'r'); we read it in _cmd_agents_run
        # by stripping whitespace + empty lines.
        content = ns.stack_files.read()
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        assert lines == ["a.py", "b.py", "c.py"]

    def test_stack_repos_flag_parses(
        self, tmp_path: Path,
    ) -> None:
        """`--stack-repos a,b,c` parses into a comma-separated
        string passed through to the orchestrator."""
        from harness.cli import _build_parser
        p = _build_parser()
        ns = p.parse_args([
            "agents", "run", "code", "t",
            "--stack-repos", f"{tmp_path}/a,{tmp_path}/b",
            "--split-into", "2",
            "--pr", "--background",
        ])
        # The parser doesn't split on comma — _cmd_agents_run does.
        # The flag value is the raw string.
        assert ns.stack_repos == f"{tmp_path}/a,{tmp_path}/b"


# === Phase 2.5: cross-repo stacks ===

class TestCrossRepoStacks:
    """Cross-repo stacks: 1 task = N PRs in N repos.

    These tests exercise the ``stack_repos`` path in
    :meth:`_run_stack_phase`. The orchestrator row's
    ``stack_repos`` column gets a JSON list of paths; each
    slice's ``_commit_slice`` / ``_push_branch`` / ``create_pr``
    / child-row ``repo`` all use the per-slice repo path.
    """

    async def test_cross_repo_two_repos_two_slices(
        self, git_repo: Path, tmp_path: Path,
    ) -> None:
        """2 repos × 2 slices → orchestrator row has
        ``stack_repos`` JSON, 2 child rows each with their
        own ``repo``."""
        # Two real git repos (so WorktreeSession.__init__ doesn't
        # blow up on the path validation it does internally).
        repo_a = git_repo  # reused fixture
        repo_b = tmp_path / "repo_b"
        repo_b.mkdir()
        await _git_init(repo_b)

        # Create a JobStore + queue.
        from harness.agents.merge_queue import MergeQueue
        from harness.agents.runner import AgentRunner
        from harness.agents.verify import AdversarialVerify

        store = JobStore(tmp_path / "jobs.db")
        runner = AgentRunner.__new__(AgentRunner)
        runner.repo = repo_a
        runner.completion_calls = 0
        queue = MergeQueue(
            runner=runner,
            verifier=AdversarialVerify.__new__(AdversarialVerify),
            store=store,
        )

        # Mock all I/O at the queue level. The orchestrator's
        # planner is patched directly so we get a deterministic
        # 2-slice plan regardless of strategy settings.
        from harness.agents import merge_queue as mq_mod
        from harness.agents.pr_split import SplitSlice
        real_plan = mq_mod.plan_splits

        def fake_plan(*args, **kwargs):
            return [
                SplitSlice(
                    position=0, files=["a.py"],
                    branch_name="harness/wt-cross/step-0",
                    title="step 1/2",
                ),
                SplitSlice(
                    position=1, files=["b.py"],
                    branch_name="harness/wt-cross/step-1",
                    title="step 2/2",
                ),
            ]
        # Module-level attribute assignment — same effect as a
        # module-level import lookup. Restored in finally.
        mq_mod.plan_splits = fake_plan  # type: ignore[assignment]

        # 2 files in 2 different "directory roots" — the files
        # strategy round-robins by file count, so 2 files ×
        # max_per_slice=1 = 2 slices.
        async def fake_get_diff(repo: Path, base: str) -> list[str]:
            if repo == repo_b:
                return ["b.py"]
            return ["a.py"]

        # Track per-slice repo and use ``MagicMock`` for the
        # spec (same as the existing ``test_three_slices`` —
        # the orchestrator never invokes the spec here).
        slice_calls: list[tuple[int, Path]] = []

        async def fake_commit(
            repo: Path, branch: str, files: list[str], msg: str,
        ) -> bool:
            slice_calls.append((len(slice_calls), repo))
            return True

        async def fake_push(repo: Path, branch: str) -> bool:
            return True

        created_numbers = iter([10, 11])
        async def fake_create_pr(
            *, repo: Path, head_branch: str, base_branch: str,
            title: str, body: str, draft: bool, env_var: str = "GITHUB_TOKEN",
        ) -> PRCreateResult:
            n = next(created_numbers)
            return PRCreateResult(
                url=f"https://gh/x/pull/{n}", number=n, branch=head_branch,
            )

        queue._get_diff_files = fake_get_diff  # type: ignore[method-assign]
        queue._commit_slice = fake_commit  # type: ignore[method-assign]
        queue._push_branch = fake_push  # type: ignore[method-assign]
        original_create_pr = mq_mod.create_pr
        mq_mod.create_pr = fake_create_pr  # type: ignore[assignment]
        mq_mod.add_pr_label = AsyncMock(return_value=None)  # type: ignore[assignment]

        try:
            job = MergeJob(
                code_spec=MagicMock(), review_spec=MagicMock(),
                task="refactor X", worktree_id="wt-cross",
                pr_mode="draft", split_into=2,
                stack_repos=[repo_a, repo_b],
            )
            job_id = await store.create(
                worktree_id=job.worktree_id, model="m", prompt=job.task,
                status="running_code",
            )
            result = await queue._run_stack_phase(
                job, job_id, repo_a, worktree_branch="harness/wt-cross",
                cost_so_far=0.0,
            )
            # Orchestrator row is not yet merged (waits for
            # children via webhook).
            assert result == (False, None, None, False)
        finally:
            mq_mod.create_pr = original_create_pr  # type: ignore[assignment]
            mq_mod.plan_splits = real_plan  # type: ignore[assignment]

        # 2 slices were committed, each in its own repo.
        assert len(slice_calls) == 2
        repos_seen = {r for _, r in slice_calls}
        assert repo_a in repos_seen
        assert repo_b in repos_seen
        # 1 orchestrator + 2 children.
        children = await store.find_jobs_by_stack_id(
            (await store.load(job_id)).pr_stack_id,  # type: ignore[union-attr]
        )
        assert len(children) == 3
        child_repos = {c.repo for c in children if c.stack_position >= 1}
        assert str(repo_a) in child_repos
        assert str(repo_b) in child_repos
        # Orchestrator row has stack_repos JSON-encoded.
        orch = next(c for c in children if c.stack_position == 0)
        assert orch.stack_repos is not None
        assert len(orch.stack_repos) == 2

    async def test_cross_repo_validation_mismatch(
        self, git_repo: Path, tmp_path: Path,
    ) -> None:
        """``len(stack_repos) != split_into`` → job fails
        with a clear error (no PRs created)."""
        from harness.agents.merge_queue import MergeQueue
        from harness.agents.runner import AgentRunner
        from harness.agents.verify import AdversarialVerify
        store = JobStore(tmp_path / "jobs.db")
        runner = AgentRunner.__new__(AgentRunner)
        runner.repo = git_repo
        runner.completion_calls = 0
        queue = MergeQueue(
            runner=runner,
            verifier=AdversarialVerify.__new__(AdversarialVerify),
            store=store,
        )

        async def fake_get_diff(repo: Path, base: str) -> list[str]:
            # Planner produces 2 slices (1 file each).
            return ["a.py", "b.py"]

        queue._get_diff_files = fake_get_diff  # type: ignore[method-assign]

        job = MergeJob(
            code_spec=MagicMock(), review_spec=MagicMock(),
            task="t", worktree_id="wt-mismatch",
            pr_mode="draft", split_into=2,
            # 3 paths, planner will produce 2 slices → mismatch.
            stack_repos=[git_repo, git_repo, git_repo],
        )
        job_id = await store.create(
            worktree_id=job.worktree_id, model="m", prompt=job.task,
            status="running_code",
        )
        result = await queue._run_stack_phase(
            job, job_id, git_repo, worktree_branch="h",
            cost_so_far=0.0,
        )
        assert result == (False, None, None, False)
        rec = await store.load(job_id)
        assert rec is not None
        assert rec.status == "failed"
        assert "mismatch" in (rec.error or "").lower()


async def _git_init(repo: Path) -> None:
    """Run ``git init`` in a temp dir for the test fixture."""
    import subprocess
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "harness@example.com"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Harness"],
        cwd=str(repo), check=True, capture_output=True,
    )
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
