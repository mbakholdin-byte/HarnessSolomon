"""Tests for harness.agents.registry (Phase 2.0, Step 2).

Covers:
  - load_agent / list_agents / all_specs with the 4 built-ins
  - Project override shadows built-in deterministically
  - Unknown names raise FileNotFoundError
  - Malformed overrides raise FrontmatterParseError (do not silently fall back)
  - Non-kebab-case files (e.g. README.md) are ignored
  - Kebab-case filter works on both built-in directory and override dir
  - has_override, builtin_only
  - Resource access via importlib.resources works in this layout
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from harness.agents.registry import (
    BUILTIN_DIR_RESOURCE,
    _builtin_file,
    all_specs,
    builtin_only,
    has_override,
    list_agents,
    load_agent,
)
from harness.agents.spec import (
    AgentSpec,
    FrontmatterParseError,
)


# === Built-in resolution ===

def test_list_agents_has_four_builtins(tmp_path: Path) -> None:
    """In a project with no overrides, only the 4 built-ins are listed."""
    names = list_agents(project_root=tmp_path)
    assert names == ["code", "explore", "plan", "review"]


def test_builtin_only_has_four_names() -> None:
    """builtin_only() never reads the project root."""
    assert sorted(builtin_only()) == ["code", "explore", "plan", "review"]


def test_load_agent_builtin_when_no_override(tmp_path: Path) -> None:
    spec = load_agent("explore", project_root=tmp_path)
    assert isinstance(spec, AgentSpec)
    assert spec.name == "explore"
    assert spec.permissions == "read-only"
    assert spec.max_iterations == 8


def test_load_agent_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no sub-agent named"):
        load_agent("nonexistent", project_root=tmp_path)


def test_all_specs_returns_four(tmp_path: Path) -> None:
    specs = all_specs(project_root=tmp_path)
    assert set(specs.keys()) == {"code", "explore", "plan", "review"}
    assert all(isinstance(s, AgentSpec) for s in specs.values())


def test_builtin_file_resource_exists() -> None:
    """``importlib.resources`` finds the .md files in the package."""
    f = _builtin_file("explore")
    assert f is not None
    assert f.is_file()
    assert f.name == "explore.md"


def test_builtin_file_resource_missing_returns_none() -> None:
    f = _builtin_file("does-not-exist")
    assert f is None


# === Project overrides ===

def test_override_shadows_builtin(tmp_path: Path) -> None:
    """A user file at ``.harness/agents/explore.md`` replaces the built-in."""
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "explore.md").write_text(
        "---\nname: explore\nmax_iterations: 12\n---\nCustom explore prompt.\n",
        encoding="utf-8",
    )
    spec = load_agent("explore", project_root=tmp_path)
    assert spec.max_iterations == 12
    assert spec.system_prompt == "Custom explore prompt."


def test_override_does_not_affect_other_names(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "explore.md").write_text(
        "---\nname: explore\n---\nCustom.\n", encoding="utf-8"
    )
    plan_spec = load_agent("plan", project_root=tmp_path)
    assert plan_spec.max_iterations == 10  # built-in value


def test_override_can_add_new_agent(tmp_path: Path) -> None:
    """A new agent in the override dir is listed alongside built-ins."""
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "custom.md").write_text(
        "---\nname: custom\ntools: [read_file]\n---\nCustom agent.\n",
        encoding="utf-8",
    )
    names = list_agents(project_root=tmp_path)
    assert "custom" in names
    spec = load_agent("custom", project_root=tmp_path)
    assert spec.name == "custom"


def test_override_can_disable_worktree(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "code.md").write_text(
        "---\nname: code\nworktree_required: false\n---\nNo worktree.\n",
        encoding="utf-8",
    )
    spec = load_agent("code", project_root=tmp_path)
    assert spec.worktree_required is False


def test_malformed_override_raises(tmp_path: Path) -> None:
    """A bad override is reported to the user, not silently replaced."""
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "explore.md").write_text(
        "no frontmatter at all", encoding="utf-8"
    )
    with pytest.raises(FrontmatterParseError, match="missing the required"):
        load_agent("explore", project_root=tmp_path)


def test_malformed_override_skipped_in_all_specs(tmp_path: Path, caplog) -> None:
    """A single bad file does not poison the whole registry."""
    import logging

    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "broken.md").write_text("nope", encoding="utf-8")
    (agents_dir / "good.md").write_text(
        "---\nname: good\n---\nA good agent.\n", encoding="utf-8"
    )
    with caplog.at_level(logging.ERROR, logger="harness.agents.registry"):
        specs = all_specs(project_root=tmp_path)
    # good.md is loaded, broken.md is skipped with a logged error.
    assert "good" in specs
    assert "broken" not in specs
    assert any("broken" in rec.message for rec in caplog.records)


# === Kebab-case filter (README etc.) ===

def test_readme_in_override_dir_is_ignored(tmp_path: Path) -> None:
    """``README.md`` in the override dir is documentation, not an agent."""
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "README.md").write_text("# docs\n", encoding="utf-8")
    names = list_agents(project_root=tmp_path)
    assert "README" not in names
    # Built-ins still present.
    assert {"code", "explore", "plan", "review"}.issubset(set(names))


def test_dotfile_in_override_dir_is_ignored(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / ".hidden.md").write_text(
        "---\nname: hidden\n---\nsecret\n", encoding="utf-8"
    )
    names = list_agents(project_root=tmp_path)
    assert "hidden" not in names


def test_non_md_in_override_dir_is_ignored(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "scratch.txt").write_text("not an agent", encoding="utf-8")
    names = list_agents(project_root=tmp_path)
    # scratch.txt is not listed.
    assert "scratch" not in names


def test_empty_override_dir_falls_back_to_builtins(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    names = list_agents(project_root=tmp_path)
    assert names == ["code", "explore", "plan", "review"]


def test_missing_override_dir_falls_back_to_builtins(tmp_path: Path) -> None:
    """No ``.harness/agents/`` at all → just the 4 built-ins."""
    assert (tmp_path / ".harness").exists() is False
    names = list_agents(project_root=tmp_path)
    assert names == ["code", "explore", "plan", "review"]


# === has_override ===

def test_has_override_true_when_present(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "explore.md").write_text(
        "---\nname: explore\n---\nCustom.\n", encoding="utf-8"
    )
    assert has_override("explore", project_root=tmp_path) is True


def test_has_override_false_when_absent(tmp_path: Path) -> None:
    assert has_override("explore", project_root=tmp_path) is False
    agents_dir = tmp_path / ".harness" / "agents"
    agents_dir.mkdir(parents=True)
    assert has_override("explore", project_root=tmp_path) is False


# === Built-in content sanity (guards against accidental edits) ===

def test_explore_built_in_is_read_only() -> None:
    spec = load_agent("explore", project_root=Path("C:/nowhere"))
    assert spec.permissions == "read-only"
    assert "write_file" not in spec.tools
    assert "edit_file" not in spec.tools


def test_code_built_in_has_full_permissions() -> None:
    spec = load_agent("code", project_root=Path("C:/nowhere"))
    assert spec.permissions == "full"
    assert "write_file" in spec.tools
    assert "edit_file" in spec.tools


def test_review_built_in_is_read_only() -> None:
    spec = load_agent("review", project_root=Path("C:/nowhere"))
    assert spec.permissions == "read-only"


def test_plan_built_in_is_read_only() -> None:
    spec = load_agent("plan", project_root=Path("C:/nowhere"))
    assert spec.permissions == "read-only"
    assert spec.max_iterations == 10


# === Edge: nonexistent project_root still loads built-ins ===

def test_load_built_in_with_nonexistent_project_root() -> None:
    """``project_root`` only affects override lookup — built-ins must work
    even when the path does not exist."""
    spec = load_agent("explore", project_root=Path("C:/this/does/not/exist/anywhere"))
    assert spec.name == "explore"
