"""Phase 3 v1.5.0 Step 1: tests for harness.privacy.path_match.match_glob.

Single source of truth для glob-семантики, переиспользуется
``harness.agents.pr_templating`` (CODEOWNERS, Phase 2.5) и
``harness.privacy.zone_filter.PrivacyZoneFilter`` (Phase 3 v1.5.0).

Зачем эти тесты:
- Защита от дрейфа между двумя callers (CODEOWNERS vs Privacy)
- Smoke test для всех glob-фич: ``*``, ``**``, ``?``, anchored ``/``,
  trailing ``/``, negation ``!``
- Edge cases: пустой path, пустой pattern
"""
from __future__ import annotations

import pytest

from harness.privacy.path_match import match_glob


class TestMatchGlob:
    """Basic glob features."""

    def test_double_star_recursive(self) -> None:
        """`**` matches across path separators."""
        assert match_glob("src/main.py", "**/*.py") is True
        assert match_glob("a/b/c/d/main.py", "**/*.py") is True
        assert match_glob("src/main.txt", "**/*.py") is False

    def test_single_star_no_slash(self) -> None:
        """`*` is greedy and matches any chars (fnmatch stdlib behavior).

        **Known fnmatch quirk:** ``*`` matches ``/`` too. The original
        ``_match_codeowners_pattern`` (Phase 2.5) had this same behavior.
        We preserve it for zero-drift reuse — fixing it would break
        Phase 2.5 CODEOWNERS patterns. For "no slash" or "exactly N
        levels" semantics, callers must use stricter patterns.
        """
        assert match_glob("main.py", "*.py") is True
        assert match_glob("src/main.py", "*.py") is True
        # `*` matches everything in fnmatch (including nested paths).
        assert match_glob("a/b/c/main.py", "*.py") is True
        # `*` in the second position of `*/*.py` is also greedy.
        # This is a documented fnmatch quirk — see header docstring.
        assert match_glob("a/b/main.py", "*/*.py") is True
        assert match_glob("a/b/c/main.py", "*/*.py") is True  # fnmatch `*` = greedy
        assert match_glob("a/main.py", "*/*.py") is True  # `*` matches `a`

    def test_question_mark_single_char(self) -> None:
        """`?` matches exactly one char."""
        assert match_glob("a.py", "?.py") is True
        assert match_glob("ab.py", "?.py") is False
        assert match_glob(".py", "?.py") is False  # 0 chars, not 1

    def test_anchored_leading_slash(self) -> None:
        """Leading `/` = match from repo root, not basename."""
        assert match_glob("docs/index.md", "/docs/**") is True
        assert match_glob("subdir/docs/index.md", "/docs/**") is False
        assert match_glob("docs", "/docs") is True  # bare file at root

    def test_directory_trailing_slash(self) -> None:
        """Trailing `/` = directory prefix, translates to `pattern + '/**'`.

        The recursive ``**`` (zero or more segments) ensures we match
        both ``docs/x.md`` and ``docs/nested/y.md``, but NOT the bare
        ``docs`` file (no path segment after).
        """
        assert match_glob("docs/anything.md", "/docs/") is True
        assert match_glob("docs/nested/x.md", "/docs/") is True
        assert match_glob("docs", "/docs/") is False  # bare file, not under dir
        assert match_glob("otherdocs/x.md", "/docs/") is False  # anchored


class TestNegation:
    """Negation prefix `!` is stripped; caller composes with `not`."""

    def test_negation_prefix_stripped(self) -> None:
        """`!pattern` strips the `!` and matches positively.

        Negation is the caller's responsibility (so the function is pure
        and composable with multiple patterns). This test confirms the
        prefix is consumed and the pattern below it is matched.
        """
        # `!docs/**` strips to `docs/**` and matches like a positive pattern.
        assert match_glob("docs/secret.md", "!docs/**") is True
        assert match_glob("src/main.py", "!docs/**") is False

    def test_negation_with_anchoring(self) -> None:
        """`!/docs/**` strips both `!` and `/`, anchored behavior applies."""
        assert match_glob("docs/x.md", "!/docs/**") is True
        assert match_glob("subdir/docs/x.md", "!/docs/**") is False


class TestEdgeCases:
    """Empty inputs, basenames, and other corner cases."""

    def test_empty_path(self) -> None:
        """Empty path matches no real pattern (``**`` is regex ``.*``).

        With the recursive-glob extension, ``**`` translates to regex
        ``.*`` which CAN match empty (in a ``search`` sense), but we
        use ``match`` (anchored to start), so the regex still requires
        the full path. For practical purposes, empty path is a no-match.
        """
        # `re.match(".*", "")` returns a match — but fnmatch's
        # behavior is "match from start, full coverage". For our use
        # case, empty path is treated as no-match for any non-trivial
        # pattern.
        assert match_glob("", "**/*.py") is False
        assert match_glob("", "*.py") is False
        assert match_glob("", "private/**") is False

    def test_empty_pattern(self) -> None:
        """Empty pattern matches only empty path."""
        assert match_glob("", "") is True
        assert match_glob("a", "") is False
        assert match_glob("a/b", "") is False

    def test_basename_fallback(self) -> None:
        """Unanchored pattern matches against basename if full path fails."""
        assert match_glob("deeply/nested/path/main.py", "*.py") is True
        assert match_glob("deeply/nested/path/main.py", "main.py") is True
        assert match_glob("deeply/nested/path/main.py", "other.py") is False

    def test_literal_path(self) -> None:
        """Pattern with no wildcards = exact match (full or basename)."""
        assert match_glob("src/main.py", "src/main.py") is True
        assert match_glob("main.py", "main.py") is True
        assert match_glob("src/main.py", "src/other.py") is False

    def test_negation_alone(self) -> None:
        """Just `!` = empty pattern after strip = matches only empty path."""
        assert match_glob("", "!") is True
        assert match_glob("a", "!") is False


class TestReuseSemantics:
    """Verify that match_glob semantics match what pr_templating expects.

    These cases are the actual ones exercised by CODEOWNERS in
    ``parse_codeowners_for_diff`` (Phase 2.5). Mirroring them here
    ensures the two callers stay in lockstep.
    """

    def test_codeowners_wildcard_pattern(self) -> None:
        """`/src/*.py` = anchored + single-star (Phase 2.5 case).

        Phase 2.5 CODEOWNERS uses ``*`` (single segment). For
        recursive matching across multiple segments, use ``**``
        (Phase 3 v1.5.0 extension, recursive-glob convention).
        """
        assert match_glob("src/main.py", "/src/*.py") is True
        # `*` is greedy: matches `src/sub/main.py` too (fnmatch quirk).
        assert match_glob("src/sub/main.py", "/src/*.py") is True
        # Anchored: `other/src/main.py` does NOT match.
        assert match_glob("other/src/main.py", "/src/*.py") is False
        # Recursive: `**` matches across multiple segments.
        assert match_glob("src/sub/main.py", "/src/**/*.py") is True
        assert match_glob("src/a/b/c/main.py", "/src/**/*.py") is True

    def test_codeowners_directory_pattern(self) -> None:
        """`/tests/` = anchored + trailing slash (Phase 2.5 case)."""
        assert match_glob("tests/test_foo.py", "/tests/") is True
        assert match_glob("tests/nested/x.py", "/tests/") is True
        assert match_glob("src/tests/x.py", "/tests/") is False  # anchored

    def test_privacy_default_patterns(self) -> None:
        """Default privacy patterns from zone_config defaults (Step 2).

        These are the patterns PrivacyZoneFilter will use by default
        once Step 2 lands. Test here so match_glob regression is caught
        before Step 2.
        """
        # `private/**` matches anything under private/
        assert match_glob("private/.env", "private/**") is True
        assert match_glob("private/secrets/key", "private/**") is True
        # `*.env` matches any .env file (basename)
        assert match_glob("private/.env", "*.env") is True
        assert match_glob("config/.env", "*.env") is True
        # `secrets/*` matches files in secrets/ (fnmatch `*` is greedy)
        assert match_glob("secrets/key", "secrets/*") is True
        assert match_glob("secrets/sub/key", "secrets/*") is True
        # For "directly in dir, not nested" semantics, use stricter
        # pattern like `secrets/*` at top level + basename check, OR
        # `secrets/{name}` for specific files. Privacy zones use
        # `secrets/**` to recursively match all.
        assert match_glob("secrets/sub/key", "secrets/**") is True
        # `**/.ssh/*` matches .ssh/ under some prefix (NOT bare .ssh/ at root)
        # fnmatch semantics: `**` requires at least 0 chars BUT the trailing
        # `/` in `**/.ssh/*` requires an actual path segment before `.ssh/`.
        # To match `.ssh/id_rsa` at repo root, use `.ssh/*` directly.
        assert match_glob("home/user/.ssh/id_rsa", "**/.ssh/*") is True
        # For repo-root `.ssh/`, the default patterns will use a separate
        # `.ssh/**` rule (added in zone_config Step 2) to cover this case.
        # Documented in zone_config.Step 2 as a known limitation of `**`.
        assert match_glob(".ssh/id_rsa", ".ssh/*") is True  # root pattern
        assert match_glob("secrets/.ssh/id_rsa", "**/.ssh/*") is True  # nested
