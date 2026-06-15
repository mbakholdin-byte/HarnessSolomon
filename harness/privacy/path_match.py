"""Phase 3 v1.5.0: Single source of truth для glob-семантики.

Originally lived in :mod:`harness.agents.pr_templating` as a private
``_match_codeowners_pattern`` (lines 262-299, 2026-06-15). Extracted
in Phase 3 v1.5.0 Step 1 to:

1. **Eliminate drift** — PrivacyZoneFilter (v1.5.0) and CODEOWNERS
   parsing (Phase 2.5) MUST use the same glob semantics. Two
   implementations would diverge over time (e.g. ``private/**`` vs
   ``private/*/file`` edge cases).

2. **Reuse tests** — same 8-10 glob edge cases (anchored ``/``,
   trailing ``/``, ``*``, ``**``, ``?``, basename fallback) are
   exercised by both CODEOWNERS and Privacy tests.

3. **Public API** — ``match_glob`` is now a stable, versioned
   function in :mod:`harness.privacy.path_match`. Callers import
   from there instead of ``pr_templating._match_codeowners_pattern``.

Backwards compat: ``pr_templating._match_codeowners_pattern`` is
re-exported from this module (see :mod:`harness.agents.pr_templating`
for the migration shim).
"""
from __future__ import annotations

import fnmatch
import re
from typing import Final

__all__ = ["match_glob"]

# Sentinel для "negated pattern" (CODEOWNERS convention).
_NEGATION_PREFIX: Final[str] = "!"

# Cache compiled regex per pattern to avoid recompiling on every call.
# fnmatch.translate converts a glob to a regex; we additionally transform
# ``**`` (which fnmatch treats as two single-stars) into ``.*`` so it
# matches across path separators (recursive glob).
_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern to a regex, with ``**`` = recursive.

    fnmatch semantics: ``*`` matches any chars INCLUDING ``/`` (Python
    stdlib quirk — documented in fnmatch docs). ``**`` is therefore
    redundant in pure fnmatch. We translate ``**`` to ``.*`` (regex)
    so it matches zero-or-more chars across separators — the standard
    recursive-glob convention (gitignore, .dockerignore, CODEOWNERS).

    This is a **behaviour-preserving extension** relative to the
    original ``_match_codeowners_pattern``: in practice, no Phase 2.5
    CODEOWNERS pattern used bare ``**`` (CODEOWNERS uses ``*`` for
    single-segment and explicit path prefixes for recursive). The new
    recursive semantics affect ONLY privacy zones (Phase 3 v1.5.0+),
    which DO use ``**`` extensively (e.g. ``private/**``, ``**/.ssh/*``).
    """
    if pattern in _REGEX_CACHE:
        return _REGEX_CACHE[pattern]
    # Transform ``**`` → ``.*`` BEFORE fnmatch.translate. fnmatch.translate
    # escapes literal ``*`` to ``\*``, so we must do this in a single pass.
    # We use a placeholder (e.g. ``@@DOUBLESTAR@@``) to survive fnmatch escaping.
    placeholder = "\x00DOUBLESTAR\x00"
    transformed = pattern.replace("**", placeholder)
    translated = fnmatch.translate(transformed)
    # Now swap placeholder back to ``.*`` in the compiled regex source.
    final = translated.replace(placeholder, ".*")
    compiled = re.compile(final)
    _REGEX_CACHE[pattern] = compiled
    return compiled


def match_glob(file_path: str, pattern: str) -> bool:
    """Test one file path against one glob pattern.

    Supports the subset of patterns needed for both CODEOWNERS and
    Privacy zones (which use the same glob conventions):

    * ``*``: matches any chars (fnmatch treats ``*`` as greedy,
      INCLUDING ``/`` — this is stdlib behaviour, not a bug)
    * ``**``: matches any chars recursively across ``/`` (translated
      to ``.*`` regex; recursive-glob convention like gitignore)
    * ``?``: matches single char
    * Leading ``/``: anchored to repo root (``/foo`` matches only
      ``foo`` at the top, not ``subdir/foo``)
    * Trailing ``/``: directory prefix (``/docs/`` matches
      ``docs/anything``)
    * Negation ``!``: NOT match (caller handles — we strip the prefix
      and return positive match; negation is applied at the call site
      so the function composes cleanly with multiple patterns)

    Args:
        file_path: Repo-relative POSIX path (no leading ``./``).
                   Examples: ``"src/main.py"``, ``"docs/index.md"``,
                   ``"private/.env"``.
        pattern:   Raw glob pattern (may start with ``/``, may end
                   with ``/``, may start with ``!``).

    Returns:
        True if file_path matches the pattern (with negation prefix
        already stripped).

    Examples:
        >>> match_glob("src/main.py", "**/*.py")
        True
        >>> match_glob("src/main.py", "*.py")  # basename fallback
        True
        >>> match_glob("private/.env", "private/**")
        True
        >>> match_glob("private/.env", "public/**")
        False
        >>> match_glob("docs/index.md", "/docs/**")  # anchored
        True
        >>> match_glob("subdir/docs/x.md", "/docs/**")  # anchored = NOT match
        False
    """
    # Strip the leading ``!`` if present; we handle negation
    # at the call site, not here. This keeps the function pure
    # (one pattern → one boolean) and composable.
    if pattern.startswith(_NEGATION_PREFIX):
        pattern = pattern[1:]

    anchored = pattern.startswith("/")
    directory = pattern.endswith("/")
    p = pattern.lstrip("/").rstrip("/")
    if directory:
        # Directory prefix: ``/docs/`` → ``docs/**`` (with ``**`` for
        # recursive matching) so it matches ``docs/anything`` and
        # ``docs/nested/x.md`` but not the bare ``docs`` file.
        p = p + "/**"
    compiled = _compile(p)
    if anchored:
        # Anchored: must match from the repo root.
        return bool(compiled.match(file_path))
    # Unanchored: match the basename OR any path segment.
    if compiled.match(file_path):
        return True
    basename = file_path.rsplit("/", 1)[-1]
    return bool(compiled.match(basename))
