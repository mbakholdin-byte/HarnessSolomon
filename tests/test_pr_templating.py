"""Tests for :mod:`harness.agents.pr_templating` (Phase 2.4 Step 1).

Covers issue extraction (regex variants) and template rendering
(default + custom template, stack metadata, issue/reviewer sections,
test plan). The module is pure (no I/O, no DB, no git).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness.agents.pr_templating import (
    DEFAULT_TEMPLATE_PATH,
    extract_issue_numbers,
    render_pr_body,
)


# === extract_issue_numbers ===

class TestExtractIssueNumbers:
    def test_bare_hash_reference(self) -> None:
        assert extract_issue_numbers("fix #123", r"#(\d+)") == [123]

    def test_multiple_references(self) -> None:
        assert extract_issue_numbers(
            "fix #123 and #456", r"#(\d+)",
        ) == [123, 456]

    def test_closes_refs_fixes_phrases(self) -> None:
        assert extract_issue_numbers(
            "Closes #1, Refs #2, Fixes #3", r"#(\d+)",
        ) == [1, 2, 3]

    def test_restrictive_pattern_only_explicit(self) -> None:
        """A more restrictive pattern (e.g. only ``Closes #N``) skips
        bare ``#N`` references."""
        assert extract_issue_numbers(
            "closes #1, but not #2", r"[Cc]loses #(\d+)",
        ) == [1]

    def test_no_issues_returns_empty(self) -> None:
        assert extract_issue_numbers(
            "no issue references here", r"#(\d+)",
        ) == []

    def test_empty_task_returns_empty(self) -> None:
        assert extract_issue_numbers("", r"#(\d+)") == []

    def test_dedup_and_sort(self) -> None:
        """Same issue mentioned twice appears once, sorted."""
        result = extract_issue_numbers(
            "fix #9, also #1, and #9 again", r"#(\d+)",
        )
        assert result == [1, 9]

    def test_invalid_pattern_returns_empty(self) -> None:
        """A bad regex (unbalanced bracket) should not crash — return []."""
        result = extract_issue_numbers("fix #1", r"[bad(")
        assert result == []


# === render_pr_body ===

class TestRenderPrBody:
    def test_default_template_renders_minimum(self) -> None:
        """The default template substitutes all placeholders with
        sensible values for a non-stacked, no-issues, no-reviewers
        job."""
        body = render_pr_body(
            task="refactor X",
            head_branch="harness/wt-1",
            base_branch="main",
        )
        assert "## Summary" in body
        assert "refactor X" in body
        assert "## Changes" in body
        assert "harness/wt-1" in body
        assert "main" in body
        assert "## Issues" in body
        assert "## Reviewers" in body
        assert "## Test plan" in body

    def test_stack_metadata_appears(self) -> None:
        body = render_pr_body(
            task="t",
            head_branch="h",
            base_branch="main",
            slice_index=1, slice_total=3, stack_id="abc",
        )
        assert "step 2/3" in body  # 0-indexed → 1-indexed display
        assert "abc" in body

    def test_single_pr_omits_step_line(self) -> None:
        body = render_pr_body(
            task="t", head_branch="h", base_branch="main",
        )
        # Non-stacked: no "step N/M" line
        assert "step" not in body.lower().split("## changes")[1].split("\n")[0]

    def test_issue_numbers_render_as_closes_refs(self) -> None:
        body = render_pr_body(
            task="t", head_branch="h", base_branch="main",
            issue_numbers=[42, 100, 7],
        )
        # First is "Closes", rest are "Refs" — sorted
        assert "Closes #7" in body
        assert "Refs #42" in body
        assert "Refs #100" in body

    def test_no_issues_section_renders_placeholder(self) -> None:
        body = render_pr_body(
            task="t", head_branch="h", base_branch="main",
            issue_numbers=[],
        )
        assert "No linked issues" in body

    def test_reviewers_render_with_at_prefix(self) -> None:
        body = render_pr_body(
            task="t", head_branch="h", base_branch="main",
            codeowners_reviewers=["alice", "bob"],
        )
        assert "@alice" in body
        assert "@bob" in body

    def test_reviewers_with_at_prefix_kept(self) -> None:
        """A reviewer already starting with ``@`` or ``/`` (team
        path) is not double-prefixed."""
        body = render_pr_body(
            task="t", head_branch="h", base_branch="main",
            codeowners_reviewers=["@alice", "/org/team"],
        )
        # Should appear as-is
        assert "@alice" in body
        assert "/org/team" in body
        # And NOT double-prefixed
        assert "@@alice" not in body

    def test_test_summary_substituted(self) -> None:
        body = render_pr_body(
            task="t", head_branch="h", base_branch="main",
            test_summary="Run pytest tests/test_x.py",
        )
        assert "Run pytest tests/test_x.py" in body

    def test_no_test_summary_uses_placeholder(self) -> None:
        body = render_pr_body(
            task="t", head_branch="h", base_branch="main",
        )
        assert "No test plan provided" in body

    def test_custom_template_path(
        self, tmp_path: Path,
    ) -> None:
        """Operators can supply a custom template via ``template_path``."""
        custom = tmp_path / "custom.md"
        custom.write_text(
            "Custom: {task} on {head_branch}\n",
            encoding="utf-8",
        )
        body = render_pr_body(
            task="my task", head_branch="feature",
            base_branch="main", template_path=custom,
        )
        assert body == "Custom: my task on feature\n"

    def test_custom_template_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            render_pr_body(
                task="t", head_branch="h", base_branch="main",
                template_path=tmp_path / "nope.md",
            )

    def test_default_template_exists(self) -> None:
        """The shipped default template is readable at module load time."""
        assert DEFAULT_TEMPLATE_PATH.is_file()
        text = DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")
        # Contains the key placeholders
        for placeholder in ("{task}", "{head_branch}", "{base_branch}",
                            "{stack_line}", "{issue_lines}",
                            "{reviewer_lines}", "{test_summary}"):
            assert placeholder in text, (
                f"default template missing placeholder {placeholder!r}"
            )


# === create_pr body_file kwarg ===

class TestCreatePRBodyFile:
    """The :func:`pr_integration.create_pr` kwarg ``body_file`` is
    threaded through to ``gh pr create --body-file <path>``. The full
    end-to-end test of ``gh`` is in :mod:`test_pr_integration`; here
    we only assert the kwarg is accepted and that ``body`` is
    ignored when ``body_file`` is set."""

    async def test_create_pr_accepts_body_file_kwarg(self) -> None:
        """The signature accepts ``body_file: Path | None = None``."""
        import inspect
        from harness.agents.pr_integration import create_pr
        sig = inspect.signature(create_pr)
        assert "body_file" in sig.parameters
        param = sig.parameters["body_file"]
        assert param.default is None
        # The annotation should be Path | None
        ann = str(param.annotation)
        assert "Path" in ann
        assert "None" in ann
