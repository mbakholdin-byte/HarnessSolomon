"""Phase 4.2+ v1.9.0: Tests for built-in agent hot-reload + harness reload CLI.

Covers:
    1. ``_builtin_dir()`` — resolves ``harness/agents/builtin/`` to a Path.
    2. ``start_builtin_agent_hot_reload()`` — wires FileWatcher to builtin dir.
    3. ``_on_builtin_change()`` — validates new content via ``_read_builtin``.
    4. ``harness reload`` CLI subcommand (via ``_cmd_reload``):
       - ``all`` / ``agents`` / ``hooks`` / ``privacy`` kinds.
       - missing directories → empty result, exit 0.
       - malformed file → recorded error, exit 1.
       - ``--json`` output mode.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.agents.hot_reload import (
    _builtin_dir,
    start_builtin_agent_hot_reload,
)
from harness.agents.spec import FrontmatterParseError
from harness.watcher import (
    FileChange,
    FileChangeKind,
    reset_file_watcher,
)


@pytest.fixture(autouse=True)
def reset_singleton() -> None:
    """Reset the FileWatcher singleton before each test."""
    reset_file_watcher()
    return None


# === _builtin_dir() tests ===


class TestBuiltinDir:
    def test_resolves_to_existing_dir(self) -> None:
        """The real ``harness/agents/builtin/`` must resolve to a Path."""
        d = _builtin_dir()
        assert d is not None
        assert d.is_dir()
        # Must contain at least explore.md, plan.md, code.md, review.md.
        names = {p.name for p in d.iterdir()}
        assert "explore.md" in names
        assert "plan.md" in names

    def test_resolves_to_path_object(self) -> None:
        d = _builtin_dir()
        assert d is not None
        assert isinstance(d, Path)


# === start_builtin_agent_hot_reload() tests ===


class TestStartBuiltinAgentHotReload:
    @pytest.mark.asyncio
    async def test_starts_watcher(self) -> None:
        """Watches the real built-in dir (it exists in the package)."""
        watcher = await start_builtin_agent_hot_reload(debounce_ms=50)
        # At least one task should be active.
        assert watcher.active >= 1
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_validates_known_builtin_on_change(self) -> None:
        """Modify a built-in file and verify _on_builtin_change runs without error."""
        # Use _on_builtin_change directly with a synthetic FileChange
        # pointing at a known built-in file.
        from harness.agents.hot_reload import _on_builtin_change

        d = _builtin_dir()
        assert d is not None
        explore = d / "explore.md"
        # Sanity: built-in must be parseable.
        from harness.agents.registry import _read_builtin
        spec = _read_builtin("explore")
        assert spec is not None

        # Simulate a MODIFIED event.
        await _on_builtin_change([
            FileChange(path=explore, kind=FileChangeKind.MODIFIED),
        ])
        # No exception → pass.

    @pytest.mark.asyncio
    async def test_ignores_path_outside_builtin(self) -> None:
        from harness.agents.hot_reload import _on_builtin_change

        bogus = FileChange(
            path=Path("/some/other/explore.md"),
            kind=FileChangeKind.MODIFIED,
        )
        # Should log a warning + return (no exception).
        await _on_builtin_change([bogus])

    @pytest.mark.asyncio
    async def test_handles_deletion_with_warning(self) -> None:
        from harness.agents.hot_reload import _on_builtin_change

        d = _builtin_dir()
        assert d is not None
        deleted = FileChange(
            path=d / "explore.md",
            kind=FileChangeKind.DELETED,
        )
        # Logs a warning; no exception.
        await _on_builtin_change([deleted])


# === harness reload CLI tests (subprocess) ===


@pytest.fixture
def harness_project(tmp_path: Path) -> Path:
    """Set up a .harness/ project with valid + malformed files."""
    project = tmp_path / "proj"
    project.mkdir()
    agents_dir = project / ".harness" / "agents"
    hooks_dir = project / ".harness" / "hooks"
    privacy_dir = project / ".harness" / "privacy"
    agents_dir.mkdir(parents=True)
    hooks_dir.mkdir(parents=True)
    privacy_dir.mkdir(parents=True)
    return project


def _run_harness_reload(
    project_root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m harness reload [args]`` in ``project_root``.

    We pass the harness package directory via ``PYTHONPATH`` so the
    subprocess can ``import harness`` even though its cwd is a
    temp dir. This mirrors how a user would invoke the command
    after ``pip install -e .`` (editable install).
    """
    import os
    harness_pkg = Path(__file__).parent.parent  # tests/ → repo root
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{harness_pkg}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-m", "harness", "reload", *args],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class TestHarnessReloadAgents:
    def test_no_files(self, harness_project: Path) -> None:
        result = _run_harness_reload(harness_project, "agents")
        assert result.returncode == 0
        assert "0 loaded" in result.stdout
        assert "errors" not in result.stdout.lower() or "0" in result.stdout

    def test_valid_agent(self, harness_project: Path) -> None:
        agents_dir = harness_project / ".harness" / "agents"
        (agents_dir / "test-agent.md").write_text(
            "---\n"
            "name: test-agent\n"
            "model: MiniMax-M2.7\n"
            "tools: [read_file, grep]\n"
            "permissions: read-only\n"
            "max_iterations: 5\n"
            "worktree_required: true\n"
            "allowed_paths: []\n"
            "---\n"
            "You are a test agent.\n",
            encoding="utf-8",
        )
        result = _run_harness_reload(harness_project, "agents")
        assert result.returncode == 0
        assert "1 loaded" in result.stdout
        assert "test-agent" in result.stdout

    def test_malformed_agent_exits_1(self, harness_project: Path) -> None:
        agents_dir = harness_project / ".harness" / "agents"
        (agents_dir / "bad.md").write_text(
            "no frontmatter here",
            encoding="utf-8",
        )
        result = _run_harness_reload(harness_project, "agents")
        assert result.returncode == 1
        assert "ERROR" in result.stderr
        assert "bad" in result.stderr

    def test_json_output(self, harness_project: Path) -> None:
        agents_dir = harness_project / ".harness" / "agents"
        (agents_dir / "x.md").write_text(
            "---\nname: x\nmodel: MiniMax-M2.7\ntools: []\n"
            "permissions: read-only\nmax_iterations: 5\n"
            "worktree_required: true\nallowed_paths: []\n---\nbody\n",
            encoding="utf-8",
        )
        result = _run_harness_reload(harness_project, "agents", "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["results"][0]["kind"] == "agents"
        assert "x" in data["results"][0]["loaded"]


class TestHarnessReloadHooks:
    def test_no_files(self, harness_project: Path) -> None:
        result = _run_harness_reload(harness_project, "hooks")
        assert result.returncode == 0
        assert "0 loaded" in result.stdout

    def test_valid_hook(self, harness_project: Path) -> None:
        hooks_dir = harness_project / ".harness" / "hooks"
        (hooks_dir / "test.json").write_text(json.dumps({
            "hook_id": "h-1",
            "event": "PreToolUse",
            "transport": "builtin",
        }), encoding="utf-8")
        result = _run_harness_reload(harness_project, "hooks")
        assert result.returncode == 0
        assert "1 loaded" in result.stdout

    def test_malformed_hook(self, harness_project: Path) -> None:
        hooks_dir = harness_project / ".harness" / "hooks"
        (hooks_dir / "bad.json").write_text("not json", encoding="utf-8")
        result = _run_harness_reload(harness_project, "hooks")
        assert result.returncode == 1
        assert "ERROR" in result.stderr


class TestHarnessReloadPrivacy:
    def test_no_files(self, harness_project: Path) -> None:
        result = _run_harness_reload(harness_project, "privacy")
        assert result.returncode == 0
        assert "0 rules" in result.stdout

    def test_valid_privacy(self, harness_project: Path) -> None:
        privacy_dir = harness_project / ".harness" / "privacy"
        (privacy_dir / "zones.json").write_text(json.dumps([
            {"pattern": "private/**", "action": "block"},
            {"pattern": "*.env", "action": "redact"},
        ]), encoding="utf-8")
        result = _run_harness_reload(harness_project, "privacy")
        assert result.returncode == 0
        assert "2 rules" in result.stdout

    def test_malformed_privacy(self, harness_project: Path) -> None:
        privacy_dir = harness_project / ".harness" / "privacy"
        (privacy_dir / "bad.json").write_text("garbage", encoding="utf-8")
        result = _run_harness_reload(harness_project, "privacy")
        assert result.returncode == 1
        assert "ERROR" in result.stderr


class TestHarnessReloadAll:
    def test_all_kind_runs_all_three(self, harness_project: Path) -> None:
        # Add one file of each kind.
        (harness_project / ".harness" / "agents" / "a.md").write_text(
            "---\nname: a\nmodel: MiniMax-M2.7\ntools: []\n"
            "permissions: read-only\nmax_iterations: 5\n"
            "worktree_required: true\nallowed_paths: []\n---\nbody\n",
            encoding="utf-8",
        )
        (harness_project / ".harness" / "hooks" / "h.json").write_text(json.dumps({
            "hook_id": "h-1", "event": "PreToolUse", "transport": "builtin",
        }), encoding="utf-8")
        (harness_project / ".harness" / "privacy" / "p.json").write_text(
            json.dumps([{"pattern": "x", "action": "block"}]),
            encoding="utf-8",
        )
        result = _run_harness_reload(harness_project, "all", "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        kinds = {r["kind"] for r in data["results"]}
        assert kinds == {"agents", "hooks", "privacy"}


class TestHarnessReloadErrors:
    def test_invalid_project_root(self, tmp_path: Path) -> None:
        result = _run_harness_reload(tmp_path, "agents", "--project-root", "/nonexistent-dir-xyz")
        assert result.returncode == 2

    def test_default_kind_is_all(self, harness_project: Path) -> None:
        # No files → "all" is fine, exit 0.
        result = _run_harness_reload(harness_project)
        assert result.returncode == 0
        # Should mention all three kinds.
        assert "agents" in result.stdout
        assert "hooks" in result.stdout
        assert "privacy" in result.stdout
