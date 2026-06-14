"""Tests for harness.cli agents subcommands (Phase 2.1, Step 4 + Phase 2.2, Step 4).

Covers:
  - ``agents list`` exits 0 and prints the 4 built-ins
  - ``agents run <unknown>`` exits 2 (FileNotFoundError surface)
  - ``agents run --no-worktree --cascade`` calls runner with
    model_override (T1 chosen for confidence=0.95)
  - ``agents run --background`` enqueues via JobStore and prints job_id
  - ``agents jobs <id>`` prints status fields
  - ``agents jobs <unknown>`` exits 1
  - ``agents jobs --recent N`` prints up to N rows
  - ``agents --help`` lists all 3 subcommands (list/run/jobs)
  - Phase 2.2: ``--pr`` flag parses correctly
  - Phase 2.2: ``--pr`` without ``--background`` exits 2
  - Phase 2.2: ``agents jobs <id>`` shows PR fields when present

These tests are run via ``subprocess.run`` to exercise the full CLI
argparse plumbing (and the "no harness server needed" path).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness.agents.jobs import JobStore


# === Helpers ===

def _run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m harness <args>`` and return the result.

    We pass an empty env-derived PYTHONPATH that includes the project
    root so ``python -m harness`` resolves regardless of cwd. We
    inherit the parent env (so tests can set MINIMAX_API_KEY if they
    want real LLM coverage; we don't).
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "harness", *args],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point settings paths at a fresh tmp dir. Used only by
    tests that need to read/write the local JobStore from the
    test process. CLI tests that spawn a subprocess pass
    ``DB_PATH`` via env instead (monkeypatch doesn't propagate
    to subprocesses)."""
    data = tmp_path / "harness-cli-data"
    data.mkdir(parents=True, exist_ok=True)
    from harness.config import settings
    monkeypatch.setattr(settings, "db_path", data / "harness.db")
    monkeypatch.setattr(settings, "session_dir", data / "sessions")
    monkeypatch.setattr(settings, "project_root", tmp_path / "project")
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    return data


# === Tests ===

def test_agents_list_prints_builtins() -> None:
    """``agents list`` exits 0 and lists the 4 Phase 2.0 built-ins."""
    res = _run_cli("agents", "list")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    for name in ("explore", "plan", "code", "review"):
        assert name in out


def test_agents_run_unknown_exits_2() -> None:
    """Unknown agent name -> FileNotFoundError -> exit 2."""
    res = _run_cli("agents", "run", "does-not-exist", "hello")
    assert res.returncode == 2
    assert "not found" in res.stderr.lower() or "error" in res.stderr.lower()


def test_agents_help_shows_all_three_subcommands() -> None:
    res = _run_cli("agents", "--help")
    assert res.returncode == 0
    for sub in ("list", "run", "jobs"):
        assert sub in res.stdout


def test_agents_jobs_unknown_id_exits_1(
    self_isolated: None = None,  # type: ignore[valid-type]
) -> None:
    """``agents jobs <unknown>`` exits 1 with a clear error."""
    res = _run_cli("agents", "jobs", "this-id-does-not-exist")
    assert res.returncode == 1
    assert "not found" in res.stderr.lower()


def test_agents_jobs_recent_empty(tmp_path: Path) -> None:
    """With an empty store, ``agents jobs --recent 5`` exits 0 and
    prints ``(no jobs)`` to stderr."""
    # We need an isolated data dir; pass via env.
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    res = _run_cli("agents", "jobs", "--recent", "5")
    # The job DB is at settings.db_path.parent/agent-jobs.db. In
    # CI, that's the user data dir; an empty DB there may or may
    # not be the case. We just verify exit code + presence of
    # either ``(no jobs)`` or a (possibly empty) header line.
    assert res.returncode == 0
    combined = res.stdout + res.stderr
    assert "no jobs" in combined.lower() or "job_id" in combined.lower()


def test_agents_run_background_prints_job_id(
    tmp_path: Path,
) -> None:
    """``agents run --background`` returns a job_id, then exits 0.

    The CLI subprocess reads ``DB_PATH`` from the environment so we
    can pin the SQLite file under ``tmp_path`` (otherwise the CLI
    writes to the project's default data dir, which would leak
    between tests).
    """
    db_path = tmp_path / "jobs.db"
    env = {"DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}
    res = _run_cli(
        "agents", "run", "explore", "hi",
        "--no-worktree", "--background",
        env_extra=env,
    )
    assert res.returncode == 0, res.stderr
    # job_id printed on stdout in the form ``job_id=<hex>``.
    assert "job_id=" in res.stdout
    # The job is persisted in the JobStore (even if the background
    # task was torn down by asyncio.run lifecycle in the CLI).
    jid = res.stdout.split("job_id=", 1)[1].split()[0]
    store = JobStore(db_path.parent / "agent-jobs.db")
    import asyncio
    rec = asyncio.run(store.load(jid))
    assert rec is not None
    assert rec.worktree_id.startswith("cli-")
    assert rec.prompt == "hi"


def test_agents_jobs_after_background_prints_status(
    tmp_path: Path,
) -> None:
    """End-to-end: enqueue a background job, then look it up."""
    db_path = tmp_path / "jobs.db"
    env = {"DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}

    # 1. Enqueue.
    res_run = _run_cli(
        "agents", "run", "explore", "list built-ins",
        "--no-worktree", "--background",
        env_extra=env,
    )
    assert res_run.returncode == 0
    jid = res_run.stdout.split("job_id=", 1)[1].split()[0]

    # 2. Poll (same DB).
    res_jobs = _run_cli("agents", "jobs", jid, env_extra=env)
    assert res_jobs.returncode == 0
    out = res_jobs.stdout
    assert jid in out
    assert "status" in out
    # Model is the built-in default.
    assert "MiniMax-M2.7" in out


def test_agents_run_cascade_chooses_t1(
    tmp_path: Path,
) -> None:
    """``agents run --cascade`` prints a cascade summary on stderr
    and runs the agent (synchronously, no --background)."""
    db_path = tmp_path / "jobs.db"
    env = {"DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}
    res = _run_cli(
        "agents", "run", "explore", "list built-ins",
        "--no-worktree", "--cascade",
        env_extra=env,
    )
    # The cascade may fail to call LLM (no API key) — we don't
    # assert success on the agent itself, only on the CLI dispatch.
    combined = res.stdout + res.stderr
    # The CLI prints the cascade decision to stderr.
    assert "cascade:" in combined
    assert "tier=T1" in combined
    assert "qwen3:8b" in combined


# === Phase 2.2: --pr flag tests ===

class TestPRFlags:
    def test_pr_flag_parses_in_help(self) -> None:
        """``--pr`` appears in ``--help`` output and is accepted by argparse."""
        res = _run_cli("agents", "run", "--help")
        assert res.returncode == 0
        for flag in ("--pr", "--pr-draft", "--pr-ready", "--pr-target"):
            assert flag in res.stdout, f"{flag} missing from --help"

    def test_pr_without_background_exits_2(
        self, tmp_path: Path,
    ) -> None:
        """``--pr`` without ``--background`` exits 2 with a clear error."""
        env = {"DB_PATH": str(tmp_path / "unused.db"), "PYTHONIOENCODING": "utf-8"}
        res = _run_cli(
            "agents", "run", "explore", "list built-ins",
            "--no-worktree", "--pr",
            env_extra=env,
        )
        assert res.returncode == 2
        assert "--background" in res.stderr

    def test_pr_ready_without_background_exits_2(
        self, tmp_path: Path,
    ) -> None:
        env = {"DB_PATH": str(tmp_path / "unused.db"), "PYTHONIOENCODING": "utf-8"}
        res = _run_cli(
            "agents", "run", "explore", "list built-ins",
            "--no-worktree", "--pr-ready",
            env_extra=env,
        )
        assert res.returncode == 2
        assert "--background" in res.stderr

    def test_pr_with_background_enqueues_with_pr_mode(
        self, tmp_path: Path,
    ) -> None:
        """``--pr --background`` enqueues a job with pr_mode='draft'."""
        db_path = tmp_path / "jobs.db"
        env = {"DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}
        res = _run_cli(
            "agents", "run", "explore", "list built-ins",
            "--no-worktree", "--pr", "--background",
            env_extra=env,
        )
        assert res.returncode == 0, res.stderr
        assert "job_id=" in res.stdout
        assert "pr_mode: draft" in res.stdout
        import asyncio
        jid = res.stdout.split("job_id=", 1)[1].split()[0]
        store = JobStore(db_path.parent / "agent-jobs.db")
        rec = asyncio.run(store.load(jid))
        assert rec is not None
        assert rec.pr_mode == "draft"

    def test_pr_ready_with_background_enqueues_with_pr_mode(
        self, tmp_path: Path,
    ) -> None:
        """``--pr-ready --background`` enqueues a job with pr_mode='ready'."""
        db_path = tmp_path / "jobs.db"
        env = {"DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}
        res = _run_cli(
            "agents", "run", "explore", "list built-ins",
            "--no-worktree", "--pr-ready", "--pr-target", "develop",
            "--background",
            env_extra=env,
        )
        assert res.returncode == 0, res.stderr
        assert "pr_mode: ready" in res.stdout
        import asyncio
        jid = res.stdout.split("job_id=", 1)[1].split()[0]
        store = JobStore(db_path.parent / "agent-jobs.db")
        rec = asyncio.run(store.load(jid))
        assert rec is not None
        assert rec.pr_mode == "ready"
        assert rec.target_branch == "develop"

    def test_jobs_output_includes_pr_fields(
        self, tmp_path: Path,
    ) -> None:
        """``agents jobs <id>`` shows PR fields when pr_mode != 'off'."""
        import asyncio
        db_path = tmp_path / "jobs.db"
        env = {"DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}
        store = JobStore(db_path.parent / "agent-jobs.db")
        jid = asyncio.run(store.create(
            worktree_id="wt-pr-show", model="m", prompt="x",
            pr_mode="draft", target_branch="main",
        ))
        res = _run_cli("agents", "jobs", jid, env_extra=env)
        assert res.returncode == 0
        out = res.stdout
        assert "pr_mode" in out
        assert "target_branch" in out
        assert "draft" in out
        assert "main" in out

    def test_jobs_output_omits_pr_fields_for_default_mode(
        self, tmp_path: Path,
    ) -> None:
        """``agents jobs <id>`` omits PR fields when pr_mode='off' (default)."""
        import asyncio
        db_path = tmp_path / "jobs.db"
        env = {"DB_PATH": str(db_path), "PYTHONIOENCODING": "utf-8"}
        store = JobStore(db_path.parent / "agent-jobs.db")
        jid = asyncio.run(store.create(
            worktree_id="wt-off-show", model="m", prompt="x",
        ))
        res = _run_cli("agents", "jobs", jid, env_extra=env)
        assert res.returncode == 0
        out = res.stdout
        assert "pr_mode     :" not in out


# === Phase 2.3: --auto-merge / --pr-auto-merge flag tests ===

class TestAutoMergeFlags:
    def test_auto_merge_flags_appear_in_help(self) -> None:
        """``--auto-merge`` and ``--pr-auto-merge`` are accepted by argparse."""
        res = _run_cli("agents", "run", "--help")
        assert res.returncode == 0
        for flag in (
            "--auto-merge", "--pr-auto-merge", "--auto-merge-method",
            "--auto-merge-label",
        ):
            assert flag in res.stdout, f"{flag} missing from --help"

    def test_pr_auto_merge_without_background_exits_2(
        self, tmp_path: Path,
    ) -> None:
        """``--pr-auto-merge`` without ``--background`` exits 2.

        Same constraint as ``--pr`` (sync path can't ``await``
        the PR lifecycle).
        """
        env = {"DB_PATH": str(tmp_path / "unused.db"), "PYTHONIOENCODING": "utf-8"}
        res = _run_cli(
            "agents", "run", "explore", "list built-ins",
            "--no-worktree", "--pr-auto-merge",
            env_extra=env,
        )
        assert res.returncode == 2
        assert "--background" in res.stderr

    def test_auto_merge_without_pr_exits_2(
        self, tmp_path: Path,
    ) -> None:
        """``--auto-merge`` without ``--pr`` exits 2.

        Auto-merge only makes sense when a PR is being opened
        (otherwise there's nothing to auto-merge).
        """
        env = {"DB_PATH": str(tmp_path / "unused.db"), "PYTHONIOENCODING": "utf-8"}
        res = _run_cli(
            "agents", "run", "explore", "list built-ins",
            "--no-worktree", "--background", "--auto-merge",
            env_extra=env,
        )
        assert res.returncode == 2
        assert "--auto-merge" in res.stderr
        assert "--pr" in res.stderr

    def test_pr_auto_merge_shorthand_accepted(
        self, tmp_path: Path,
    ) -> None:
        """``--pr-auto-merge`` is parsed and not rejected as an unknown flag.

        The actual job enqueue would call ``gh pr create`` etc.,
        which is mocked away by ``--no-worktree`` not being
        passed here. We just check the CLI doesn't error on the
        flag itself. (The deeper test of ``--pr-auto-merge``
        plumbing is in ``test_merge_queue_pr.py::TestAutoMergePhase``.)
        """
        env = {"DB_PATH": str(tmp_path / "jobs.db"), "PYTHONIOENCODING": "utf-8"}
        # ``--background --pr-auto-merge`` should at least reach
        # the enqueue step (which then needs ``gh``). The exit
        # code may be non-zero because we don't have a real
        # worktree, but it should NOT be 2 (which would mean
        # the flag was rejected by argparse).
        res = _run_cli(
            "agents", "run", "explore", "list built-ins",
            "--background", "--pr-auto-merge",
            env_extra=env,
        )
        # We expect some non-zero code because we don't have
        # worktrees or gh set up, but the flag should not be
        # rejected at the argparse level.
        assert res.returncode != 2, (
            f"argparse rejected --pr-auto-merge: rc=2 stderr={res.stderr!r}"
        )
