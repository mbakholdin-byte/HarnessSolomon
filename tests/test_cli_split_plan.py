"""Tests for ``harness agents split-plan`` subcommand (Phase 2.4 Step 4).

The subcommand is a dry-run preview of how a worktree's diff would
be split into N stacked PRs. It runs the same ``plan_splits``
function as ``_run_stack_phase``, but does no git mutations, no
gh calls, no JobStore writes.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness.cli import _cmd_agents_split_plan, _build_parser
from harness.config import settings


def _parse(argv: list[str]):
    return _build_parser().parse_args(argv)


# === _cmd_agents_split_plan (direct call) ===

class TestSplitPlanDryRun:
    def test_explicit_files_prints_plan(self, tmp_path: Path) -> None:
        """``split-plan --files <list>`` plans without running git."""
        f = tmp_path / "files.txt"
        f.write_text(
            "src/a.py\nsrc/b.py\ntests/t.py\ndocs/d.md\n",
            encoding="utf-8",
        )
        ns = _parse([
            "agents", "split-plan",
            "--files", str(f),
            "--split-into", "3",
            "--strategy", "directory",
        ])
        rc = _cmd_agents_split_plan(ns)
        assert rc == 0
        # Print captured via stdout (we don't capture in this
        # test, but a manual run would show the plan).

    def test_explicit_files_auto_strategy_single_slice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """``auto`` strategy with a 2-file diff → 1 slice (the
        collapsed message is printed)."""
        f = tmp_path / "small.txt"
        f.write_text("a.py\nb.py\n", encoding="utf-8")
        ns = _parse(["agents", "split-plan", "--files", str(f)])
        rc = _cmd_agents_split_plan(ns)
        captured = capsys.readouterr()
        assert rc == 0
        assert "planner collapsed to 1 slice" in captured.out

    def test_empty_files_exits_0_with_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """An empty file list is not an error — caller decides."""
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        ns = _parse(["agents", "split-plan", "--files", str(f)])
        rc = _cmd_agents_split_plan(ns)
        captured = capsys.readouterr()
        assert rc == 0
        assert "no files" in captured.err.lower()

    def test_three_slices_prints_three_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """3 directories, ``--split-into 3`` → 3 slices printed
        with file lists."""
        f = tmp_path / "d.txt"
        f.write_text(
            "src/a.py\nsrc/b.py\n"
            "tests/t.py\n"
            "docs/d.md\n",
            encoding="utf-8",
        )
        ns = _parse([
            "agents", "split-plan",
            "--files", str(f),
            "--split-into", "3",
            "--strategy", "directory",
        ])
        rc = _cmd_agents_split_plan(ns)
        captured = capsys.readouterr()
        assert rc == 0
        # All 3 slices in output.
        assert "slice 1/3" in captured.out
        assert "slice 2/3" in captured.out
        assert "slice 3/3" in captured.out

    def test_git_diff_used_when_no_files_arg(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """No --files → runs ``git diff --name-only <base>`` and
        plans against the result. We mock ``subprocess.run``
        via the function's local namespace."""
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "a.py\nb.py\nc.py\n"
        mock_result.stderr = ""
        # The function does ``import subprocess`` inside its
        # body, so the lookup happens in its globals at call
        # time. Patch the global ``subprocess`` module.
        import subprocess as _sp
        with patch.object(_sp, "run", return_value=mock_result):
            ns = _parse([
                "agents", "split-plan", str(tmp_path),
                "--base", "main",
            ])
            rc = _cmd_agents_split_plan(ns)
        captured = capsys.readouterr()
        assert rc == 0
        # "total files: 3" header
        assert "total files: 3" in captured.out

    def test_git_diff_failure_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """``git diff`` returning non-zero → exit 2 with error."""
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        mock_result.stderr = "fatal: not a git repository"
        import subprocess as _sp
        with patch.object(_sp, "run", return_value=mock_result):
            ns = _parse([
                "agents", "split-plan", str(tmp_path),
            ])
            rc = _cmd_agents_split_plan(ns)
        assert rc == 2
        captured = capsys.readouterr()
        assert "git diff failed" in captured.err

    def test_git_not_in_path_exits_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """If git is not in PATH, the command exits 3 (graceful
        error, not a stack trace)."""
        from unittest.mock import patch
        import subprocess as _sp
        with patch.object(
            _sp, "run", side_effect=FileNotFoundError,
        ):
            ns = _parse([
                "agents", "split-plan", str(tmp_path),
            ])
            rc = _cmd_agents_split_plan(ns)
        assert rc == 3
        captured = capsys.readouterr()
        assert "git not found" in captured.err.lower()

    def test_settings_overrides_applied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        """CLI flags override settings.pr_split_strategy etc."""
        f = tmp_path / "f.txt"
        f.write_text(
            "src/a.py\nsrc/b.py\nsrc/c.py\n"
            "tests/t.py\ntests/t2.py\n"
            "docs/d.md\n",
            encoding="utf-8",
        )
        # Strategy "files" with split-into=3: round-robin into 3.
        ns = _parse([
            "agents", "split-plan",
            "--files", str(f),
            "--split-into", "3",
            "--strategy", "files",
        ])
        # Monkeypatch the max_files_per_slice so all 6 files fall
        # under one slice's cap (otherwise auto would collapse).
        # Actually, with "files" + n=3, n=3 should be respected.
        rc = _cmd_agents_split_plan(ns)
        captured = capsys.readouterr()
        assert rc == 0
        # The strategy is reflected in the output header.
        assert "'files' strategy" in captured.out


# === _cmd_agents dispatcher integration ===

class TestDispatcher:
    def test_split_plan_subcommand_dispatches(self) -> None:
        """``harness agents split-plan`` reaches
        ``_cmd_agents_split_plan`` (verified by checking the
        subcommand is registered in the parser)."""
        # argparse exits with SystemExit(0) on --help; catch it
        # to confirm the subcommand is registered.
        with pytest.raises(SystemExit) as exc:
            _parse(["agents", "split-plan", "--help"])
        assert exc.value.code == 0

    def test_split_plan_help_lists_flags(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        """`harness agents split-plan --help` lists all flags."""
        with pytest.raises(SystemExit) as exc:
            _parse(["agents", "split-plan", "--help"])
        # SystemExit(0) for --help
        assert exc.value.code == 0
        captured = capsys.readouterr()
        for flag in (
            "--base", "--split-into", "--strategy", "--files",
        ):
            assert flag in captured.out


# === End-to-end (subprocess) ===

class TestSplitPlanSubprocess:
    """Full end-to-end test via ``harness agents split-plan``."""

    def test_subprocess_split_plan_with_files(
        self, tmp_path: Path,
    ) -> None:
        """Invoke the CLI as a subprocess (matching the
        ``test_cli_agents.py`` pattern) and verify exit code +
        plan output."""
        f = tmp_path / "stack.txt"
        f.write_text(
            "src/a.py\nsrc/b.py\n"
            "tests/t.py\n"
            "docs/d.md\n",
            encoding="utf-8",
        )
        env = {
            **__import__("os").environ,
            "PYTHONIOENCODING": "utf-8",
        }
        result = subprocess.run(
            [sys.executable, "-m", "harness", "agents", "split-plan",
             "--files", str(f),
             "--split-into", "3",
             "--strategy", "directory"],
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
        )
        out = result.stdout
        assert "plan: 3 slice" in out
        assert "slice 1/3" in out
        assert "slice 2/3" in out
        assert "slice 3/3" in out
