"""Tests for ``harness.agents.pr_templating.parse_codeowners_for_diff`` (Phase 2.5)."""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.agents.pr_templating import parse_codeowners_for_diff


def _write_codeowners(repo: Path, content: str) -> None:
    """Helper: write a .github/CODEOWNERS file in ``repo``."""
    p = repo / ".github" / "CODEOWNERS"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestParseCodeownersForDiff:
    def test_no_codeowners_file_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        # No CODEOWNERS file written.
        result = parse_codeowners_for_diff(
            tmp_path, ["src/a.py", "tests/t.py"],
        )
        assert result == []

    def test_empty_diff_returns_empty(self, tmp_path: Path) -> None:
        _write_codeowners(tmp_path, "*  @octocat\n")
        assert parse_codeowners_for_diff(tmp_path, []) == []

    def test_basic_pattern_match(self, tmp_path: Path) -> None:
        _write_codeowners(tmp_path, "src/  @alice\n")
        result = parse_codeowners_for_diff(
            tmp_path, ["src/a.py", "src/b/c.py"],
        )
        assert result == ["@alice"]

    def test_basename_match(self, tmp_path: Path) -> None:
        # Unanchored patterns also match by basename (GitHub
        # behavior). ``*.py`` should match ``src/x.py`` and
        # ``tests/y.py``.
        _write_codeowners(tmp_path, "*.py  @bob\n")
        result = parse_codeowners_for_diff(
            tmp_path, ["src/x.py", "tests/y.py", "docs/readme.md"],
        )
        assert result == ["@bob"]

    def test_anchored_pattern(self, tmp_path: Path) -> None:
        # ``/docs/`` is anchored: matches only ``docs/anything``
        # at the repo root, NOT nested directories.
        _write_codeowners(tmp_path, "/docs/  @carol\n")
        result = parse_codeowners_for_diff(
            tmp_path, ["docs/intro.md", "src/docs/x.md"],
        )
        assert result == ["@carol"]

    def test_multiple_owners_per_pattern(self, tmp_path: Path) -> None:
        _write_codeowners(
            tmp_path, "src/  @alice @bob @org/team-core\n",
        )
        result = parse_codeowners_for_diff(
            tmp_path, ["src/x.py"],
        )
        assert result == ["@alice", "@bob", "@org/team-core"]

    def test_union_across_patterns(self, tmp_path: Path) -> None:
        _write_codeowners(tmp_path, (
            "src/  @alice\n"
            "tests/  @bob\n"
            "*.md  @carol\n"
        ))
        result = parse_codeowners_for_diff(
            tmp_path, ["src/a.py", "tests/t.py", "docs/readme.md"],
        )
        assert result == ["@alice", "@bob", "@carol"]

    def test_dedup_and_sort(self, tmp_path: Path) -> None:
        _write_codeowners(tmp_path, (
            "src/  @charlie @alice\n"
            "tests/  @alice @bob\n"
        ))
        result = parse_codeowners_for_diff(
            tmp_path, ["src/a.py", "tests/t.py"],
        )
        # Sorted alphabetically, dedup.
        assert result == ["@alice", "@bob", "@charlie"]

    def test_comments_and_blank_lines_ignored(
        self, tmp_path: Path,
    ) -> None:
        _write_codeowners(tmp_path, (
            "# This is a comment\n"
            "\n"
            "src/  @alice\n"
            "  # indented comment\n"
        ))
        result = parse_codeowners_for_diff(
            tmp_path, ["src/a.py"],
        )
        assert result == ["@alice"]

    def test_windows_paths_normalized(self, tmp_path: Path) -> None:
        _write_codeowners(tmp_path, "src/  @alice\n")
        # Backslashes (Windows worktrees) should be normalised
        # before matching.
        result = parse_codeowners_for_diff(
            tmp_path, ["src\\a.py"],
        )
        assert result == ["@alice"]

    def test_leading_dot_slash_stripped(
        self, tmp_path: Path,
    ) -> None:
        _write_codeowners(tmp_path, "src/  @alice\n")
        result = parse_codeowners_for_diff(
            tmp_path, ["./src/a.py"],
        )
        assert result == ["@alice"]

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        _write_codeowners(tmp_path, "src/  @alice\n")
        result = parse_codeowners_for_diff(
            tmp_path, ["docs/readme.md"],
        )
        assert result == []

    def test_owner_with_email(self, tmp_path: Path) -> None:
        # CODEOWNERS allows email addresses; we just pass them
        # through.
        _write_codeowners(tmp_path, "src/  user@example.com\n")
        result = parse_codeowners_for_diff(
            tmp_path, ["src/a.py"],
        )
        assert result == ["user@example.com"]

    def test_custom_codeowners_path(self, tmp_path: Path) -> None:
        # Override the default location.
        p = tmp_path / "CODEOWNERS"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("docs/  @dave\n", encoding="utf-8")
        result = parse_codeowners_for_diff(
            tmp_path, ["docs/x.md"],
            codeowners_path=Path("CODEOWNERS"),
        )
        assert result == ["@dave"]

    def test_empty_pattern_skipped(self, tmp_path: Path) -> None:
        # A line that has only a pattern and no owner shouldn't
        # crash — the parser drops such rows.
        _write_codeowners(tmp_path, "src/\n")
        result = parse_codeowners_for_diff(
            tmp_path, ["src/a.py"],
        )
        assert result == []
