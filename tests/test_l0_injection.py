"""Tests for Phase 3 v1.2.1 L0 → system prompt injection.

Covers:
  * ``_format_l0_section`` — empty list, single note, tags, order
  * ``build_system_prompt_for(l0_section=)`` — prepended / unchanged
  * ``ToolRuntime(l0_section=)`` — kwarg stored / default None
  * (Step 1) — see ``test_agent_loop.py`` for AgentLoop application tests
  * (Step 2) — see ``test_phase3_v1_2_1_integration.py`` for E2E
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.agents.runner import (
    L0_SECTION_HEADING,
    _format_l0_section,
    build_system_prompt_for,
)
from harness.agents.spec import AgentSpec
from harness.server.agent.runtime import ToolRuntime


# === _format_l0_section ===

class TestFormatL0Section:
    def test_empty_returns_none(self) -> None:
        """No notes → no section (caller should skip injection)."""
        assert _format_l0_section([]) is None

    def test_single_note(self) -> None:
        n = MagicMock()
        n.id = 7
        n.tags = []
        n.content = "user prefers concise replies"
        out = _format_l0_section([n])
        assert out is not None
        assert L0_SECTION_HEADING in out
        assert "id=7" in out
        assert "user prefers concise replies" in out
        # No tag marker when tags is empty
        assert "[" not in out.split("\n", 1)[1]   # heading + first bullet

    def test_with_tags(self) -> None:
        n = MagicMock()
        n.id = 1
        n.tags = ["important", "from-user"]
        n.content = "always reply in Russian"
        out = _format_l0_section([n])
        assert out is not None
        assert "[important,from-user]" in out

    def test_preserves_order_newest_first(self) -> None:
        n1 = MagicMock(); n1.id = 1; n1.tags = []; n1.content = "first"
        n2 = MagicMock(); n2.id = 2; n2.tags = []; n2.content = "second"
        n3 = MagicMock(); n3.id = 3; n3.tags = []; n3.content = "third"
        out = _format_l0_section([n1, n2, n3])
        assert out is not None
        # The position of each content string in the section preserves
        # the input order (newest first as the store returns).
        assert out.index("first") < out.index("second") < out.index("third")

    def test_multiple_notes_all_present(self) -> None:
        ns = []
        for i, content in enumerate(("a", "b", "c"), start=1):
            n = MagicMock()
            n.id = i
            n.tags = []
            n.content = content
            ns.append(n)
        out = _format_l0_section(ns)
        assert out is not None
        for content in ("a", "b", "c"):
            assert content in out

    def test_handles_missing_tags_attribute(self) -> None:
        """``getattr(n, "tags", None)`` shields against duck-typed inputs."""
        n = MagicMock(spec=["id", "content"])  # no tags attribute
        n.id = 9
        n.content = "no tags attr"
        out = _format_l0_section([n])
        assert out is not None
        assert "id=9" in out
        assert "no tags attr" in out

    def test_handles_missing_id_attribute(self) -> None:
        n = MagicMock(spec=["content", "tags"])
        n.tags = []
        n.content = "no id"
        out = _format_l0_section([n])
        assert out is not None
        assert "id=0" in out   # default from getattr


# === build_system_prompt_for(l0_section=) ===

class TestBuildSystemPromptForL0:
    def test_l0_section_prepended_to_role_first(
        self, tmp_path: Any,
    ) -> None:
        spec = AgentSpec(
            name="explore",
            system_prompt="You are the explore sub-agent.",
            tools=["read_file"],
        )
        out = build_system_prompt_for(
            spec, tmp_path, [t for t in [] if t["name"] in spec.tools],
            l0_section="## Hot context\n- (id=1) fact",
        )
        # L0 section appears BEFORE the role description (so the model
        # reads the working state first).
        assert out.index("## Hot context") < out.index("You are the explore sub-agent.")
        assert "id=1" in out

    def test_l0_section_prepended_even_without_role(self, tmp_path: Any) -> None:
        spec = AgentSpec(name="x", system_prompt="", tools=[])
        out = build_system_prompt_for(
            spec, tmp_path, [], l0_section="## Hot context\n- fact",
        )
        # L0 section appears BEFORE the standard "You are Solomon" prelude.
        assert out.index("## Hot context") < out.index("You are Solomon")

    def test_no_l0_section_unchanged(self, tmp_path: Any) -> None:
        """``l0_section=None`` is the v1.2.0 default — output identical."""
        spec = AgentSpec(name="x", system_prompt="", tools=[])
        before = build_system_prompt_for(spec, tmp_path, [])
        after = build_system_prompt_for(spec, tmp_path, [], l0_section=None)
        assert before == after
        # Sanity: the standard prelude is present.
        assert "You are Solomon" in after

    def test_empty_string_l0_section_unchanged(self, tmp_path: Any) -> None:
        """Empty string is falsy → no injection (consistent with None)."""
        spec = AgentSpec(name="x", system_prompt="", tools=[])
        out = build_system_prompt_for(spec, tmp_path, [], l0_section="")
        assert "## Hot context" not in out
        assert "You are Solomon" in out


# === ToolRuntime(l0_section=) — Step 1 surface ===

class TestRuntimeL0Kwarg:
    def test_runtime_stores_l0_section(self, tmp_path: Any) -> None:
        rt = ToolRuntime(project_root=tmp_path, l0_section="## Hot context\n- fact")
        assert rt._l0_section == "## Hot context\n- fact"  # type: ignore[attr-defined]

    def test_runtime_default_l0_section_is_none(self, tmp_path: Any) -> None:
        rt = ToolRuntime(project_root=tmp_path)
        assert rt._l0_section is None  # type: ignore[attr-defined]

    def test_runtime_l0_section_with_scratchpad(self, tmp_path: Any) -> None:
        """l0_section is orthogonal to scratchpad; both can be set."""
        fake_scratchpad = MagicMock()
        rt = ToolRuntime(
            project_root=tmp_path,
            scratchpad=fake_scratchpad,  # type: ignore[arg-type]
            l0_section="## Hot",
        )
        assert rt._scratchpad is fake_scratchpad
        assert rt._l0_section == "## Hot"
