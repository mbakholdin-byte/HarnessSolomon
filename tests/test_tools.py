"""Tests for tool runtime + safety (Шаг 4, Phase 0).

Per TDD (RED → GREEN → REFACTOR): tests first, then minimal implementation.

Tools under test (6):
  read_file, edit_file, write_file, bash, grep, glob

12 unit tests = 6 tools × 2 cases (positive + negative).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harness.config import settings
from harness.server.agent import runtime as runtime_mod
from harness.server.agent.runtime import ToolRuntime
from harness.server.agent.safety import BASH_DENY_PATTERNS, is_safe_path


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated project root for file tool tests."""
    root = tmp_path / "project"
    root.mkdir()
    # Add a sample file
    (root / "hello.txt").write_text("hello world", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.txt").write_text("nested content", encoding="utf-8")
    monkeypatch.setattr(settings, "project_root", root)
    return root


@pytest.fixture
def runtime(project_root: Path) -> ToolRuntime:
    """ToolRuntime bound to the temp project root."""
    return ToolRuntime(project_root=project_root)


# === ToolResult + ToolRegistry smoke ===

def test_tool_schemas_contains_fourteen_tools() -> None:
    """TOOL_SCHEMAS declares exactly 14 tools (6 file/shell + 8 scratchpad).

    Phase 3 v1.2.0 added 4 scratchpad tools to the original 6:
    ``scratchpad_write_note``, ``scratchpad_read_notes``,
    ``scratchpad_plan_step``, ``scratchpad_mark_done``.

    Phase 3 v1.3.0 added 2 L2 retrieval tools:
    ``scratchpad_l2_search`` (hybrid dense+BM25 + LLM-curator)
    and ``scratchpad_l2_promote_to_l1`` (hierarchical summary).

    Phase 3 v1.3.1 added 2 offload recovery tools:
    ``scratchpad_read_offloaded`` (fetch offloaded note body) and
    ``scratchpad_search_offloaded`` (semantic search across
    offloaded notes — reuses v1.3.0 ``L2Retriever``).
    """
    from harness.server.agent.tools import TOOL_SCHEMAS

    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "read_file", "edit_file", "write_file", "bash", "grep", "glob",
        "scratchpad_write_note", "scratchpad_read_notes",
        "scratchpad_plan_step", "scratchpad_mark_done",
        "scratchpad_l2_search", "scratchpad_l2_promote_to_l1",
        "scratchpad_read_offloaded", "scratchpad_search_offloaded",
    }


def test_tool_registry_register_and_get() -> None:
    """ToolRegistry stores callable by name."""
    from harness.server.agent.tools import ToolRegistry

    reg = ToolRegistry()
    fn = lambda: "ok"
    reg.register("noop", fn)
    assert reg.get("noop") is fn
    assert reg.get("missing") is None
    assert "noop" in reg.names()


# === read_file ===

async def test_read_file_positive(runtime: ToolRuntime, project_root: Path) -> None:
    """read_file returns file content."""
    result = await runtime.execute("read_file", {"path": "hello.txt"})
    assert result.ok
    assert result.output == "hello world"
    assert result.error == ""


async def test_read_file_out_of_scope(runtime: ToolRuntime, project_root: Path) -> None:
    """read_file with path outside project_root → ok=False."""
    # Use an absolute path that is definitely outside tmp project
    outside = Path("C:/Windows/System32/drivers/etc/hosts").resolve()
    result = await runtime.execute("read_file", {"path": str(outside)})
    assert not result.ok
    assert "outside" in result.error.lower() or "project_root" in result.error.lower()


# === edit_file ===

async def test_edit_file_positive(runtime: ToolRuntime, project_root: Path) -> None:
    """edit_file replaces old_string with new_string."""
    target = project_root / "hello.txt"
    result = await runtime.execute(
        "edit_file",
        {
            "path": "hello.txt",
            "old_string": "hello world",
            "new_string": "hello Mark",
        },
    )
    assert result.ok
    assert target.read_text(encoding="utf-8") == "hello Mark"


async def test_edit_file_old_string_not_found(runtime: ToolRuntime) -> None:
    """edit_file with missing old_string → error, file unchanged."""
    result = await runtime.execute(
        "edit_file",
        {
            "path": "hello.txt",
            "old_string": "DOES_NOT_EXIST",
            "new_string": "whatever",
        },
    )
    assert not result.ok
    assert "not found" in result.error.lower()


# === write_file ===

async def test_write_file_positive(runtime: ToolRuntime, project_root: Path) -> None:
    """write_file creates parents and writes content."""
    new_path = project_root / "deep" / "new" / "file.txt"
    result = await runtime.execute(
        "write_file", {"path": "deep/new/file.txt", "content": "new content"}
    )
    assert result.ok
    assert new_path.exists()
    assert new_path.read_text(encoding="utf-8") == "new content"


async def test_write_file_out_of_scope(runtime: ToolRuntime, tmp_path: Path) -> None:
    """write_file with path outside project_root → error."""
    outside = tmp_path / "outside.txt"
    result = await runtime.execute(
        "write_file", {"path": str(outside), "content": "x"}
    )
    assert not result.ok


# === bash ===

async def test_bash_positive(runtime: ToolRuntime) -> None:
    """bash runs a simple echo and returns stdout."""
    result = await runtime.execute("bash", {"command": "echo hello"})
    assert result.ok
    assert "hello" in result.output
    assert result.exit_code == 0


async def test_bash_deny_rm_rf(runtime: ToolRuntime) -> None:
    """bash('rm -rf /') is denied by safety pattern."""
    result = await runtime.execute("bash", {"command": "rm -rf /"})
    assert not result.ok
    assert "denied" in result.error.lower() or "deny" in result.error.lower()


# === grep ===

async def test_grep_positive(runtime: ToolRuntime) -> None:
    """grep finds the pattern in project files."""
    # grep on the project root, looking for "hello"
    result = await runtime.execute(
        "grep", {"pattern": "hello", "path": str(runtime.project_root)}
    )
    assert result.ok
    assert "hello.txt" in result.output or "hello" in result.output


async def test_grep_deny_dangerous_path(runtime: ToolRuntime) -> None:
    """grep with path-traversal outside project_root is denied."""
    result = await runtime.execute(
        "grep", {"pattern": "x", "path": "../../../etc"}
    )
    # We don't expose path outside root: either denied by safety or returns empty.
    # Spec says: path outside project_root → error.
    assert not result.ok


# === glob ===

async def test_glob_positive(runtime: ToolRuntime) -> None:
    """glob lists matching files."""
    result = await runtime.execute("glob", {"pattern": "**/*.txt"})
    assert result.ok
    assert "hello.txt" in result.output
    assert "nested.txt" in result.output


async def test_glob_out_of_scope(runtime: ToolRuntime) -> None:
    """glob with path outside project_root → error."""
    result = await runtime.execute("glob", {"pattern": "*", "path": "../"})
    assert not result.ok


# === safety module direct tests ===

def test_safety_deny_patterns_present() -> None:
    """At least the 5 mandatory patterns are defined."""
    joined = "\n".join(BASH_DENY_PATTERNS)
    assert "rm" in joined and "del" in joined and "format" in joined
    assert "push" in joined and "force" in joined
    assert "reset" in joined and "hard" in joined


def test_safety_is_safe_path_allows_subdir(tmp_path: Path) -> None:
    """Subdir under project_root is safe."""
    root = tmp_path / "root"
    root.mkdir()
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    assert is_safe_path(sub, root) is True
    assert is_safe_path(root, root) is True


def test_safety_is_safe_path_denies_traversal(tmp_path: Path) -> None:
    """Path that escapes project_root via '..' is denied."""
    root = tmp_path / "root"
    root.mkdir()
    outside = root / ".." / "evil.txt"
    assert is_safe_path(outside, root) is False


# Sanity: rg fallback is not required if grep tool exists.
def test_shutil_which_grep_rg() -> None:
    """At least one of rg/grep must be available (sanity)."""
    has_rg = shutil.which("rg") is not None
    has_grep = shutil.which("grep") is not None
    assert has_rg or has_grep, "neither rg nor grep found in PATH"


# Ensure module-level runtime alias exists for callers
def test_runtime_module_exports_toolruntime() -> None:
    """runtime module exports ToolRuntime (for `from ... import ToolRuntime`)."""
    assert hasattr(runtime_mod, "ToolRuntime")
