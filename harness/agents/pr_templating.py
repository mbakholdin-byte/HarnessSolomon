"""PR body templating (Phase 2.4, Step 1).

Phase 2.2 / 2.3 used a hard-coded f-string in ``merge_queue._run_pr_phase``
to build the PR body. Phase 2.4 replaces that with a real templating
layer that:

- Extracts issue numbers from the task text (so a task like
  ``"fix #123, see #456"`` automatically adds ``Closes #123`` /
  ``Refs #456`` lines).
- Inlines the stack position (``step 2/3``) for stacked PR jobs.
- Optionally renders codeowners / suggested reviewers if the operator
  supplies them.
- Supports a custom template file via
  ``settings.pr_template_path``. The template uses simple ``{var}``
  placeholders (no Jinja2 — stdlib only, 0 new deps).

**Design constraints:**

- **Pure functions, no I/O.** ``render_pr_body`` is a pure function
  that returns a string. The caller is responsible for reading the
  template file and for handling the ``body_file`` (long-body)
  optimization in :mod:`harness.agents.pr_integration`.
- **Deterministic.** Same inputs → same output. The template uses
  sorted order for reviewers and issue numbers.
- **No new deps.** Regex from ``re``, dict iteration is insertion
  order, f-strings for rendering. The template file is loaded with
  ``Path.read_text()`` only by the convenience helper.
"""
from __future__ import annotations

import fnmatch  # noqa: F401  -- kept for backwards compat (re-exported below)
import re
from pathlib import Path
from typing import Iterable

# Phase 3 v1.5.0 Step 1: glob-семантика вынесена в ``harness.privacy.path_match``
# как single source of truth. ``_match_codeowners_pattern`` оставлен как
# re-export для backwards compat (Phase 2.5 tests импортируют его).
from harness.privacy.path_match import match_glob as _match_codeowners_pattern


#: Default template path (shipped with the package). Operators can
#: override via ``settings.pr_template_path``.
DEFAULT_TEMPLATE_PATH: Path = Path(
    __file__).parent / "templates" / "pr_body.md"


# === Issue extraction ===

def extract_issue_numbers(task: str, pattern: str) -> list[int]:
    """Extract GitHub issue numbers from a task description.

    Args:
        task: The job's task text (e.g. ``"fix #123, see #456"``).
        pattern: A regular expression with one capturing group. The
            default (``r"#(\d+)"``) matches bare ``#123`` references.
            Operators can supply a more restrictive pattern like
            ``r"(?:Closes|Refs|Fixes) #(\d+)"`` to limit
            auto-linking to explicit phrases.

    Returns:
        Sorted, deduplicated list of issue numbers found in the
        task text. May be empty.

    Examples:
        >>> extract_issue_numbers("fix #123", r"#(\\d+)")
        [123]
        >>> extract_issue_numbers("Closes #1, Refs #2, Closes #1",
        ...                        r"#(\\d+)")
        [1, 2]
        >>> extract_issue_numbers("no issues here", r"#(\\d+)")
        []
    """  # noqa: W605
    if not task:
        return []
    try:
        regex = re.compile(pattern)
    except re.error as e:
        # Bad config — treat as no issues rather than crashing the
        # PR creation. The operator will see a log warning.
        import logging
        logging.getLogger(__name__).warning(
            "pr_issue_link_re is invalid (%s); ignoring", e,
        )
        return []
    return sorted({int(m) for m in regex.findall(task)})


# === Rendering ===

def render_pr_body(
    *,
    task: str,
    head_branch: str,
    base_branch: str,
    template_path: Path | None = None,
    slice_index: int | None = None,
    slice_total: int | None = None,
    stack_id: str | None = None,
    issue_numbers: Iterable[int] | None = None,
    codeowners_reviewers: Iterable[str] | None = None,
    test_summary: str = "",
) -> str:
    """Render a PR body from the template.

    Args:
        task: The job's task description (used in the ``## Summary``
            section).
        head_branch: The branch we just pushed.
        base_branch: The branch the PR targets.
        template_path: Override for the default template. ``None`` =
            use :data:`DEFAULT_TEMPLATE_PATH`.
        slice_index: 0-based position in the stack (``None`` for
            non-stacked jobs).
        slice_total: Total slices in the stack (``None`` for
            non-stacked jobs).
        stack_id: Stack identifier (``None`` for non-stacked jobs).
        issue_numbers: Issue numbers to render as ``Closes #N`` /
            ``Refs #N`` lines. ``None`` = no issue section.
        codeowners_reviewers: Reviewer usernames / team names. Renders
            as ``@user1 @user2``. ``None`` = no reviewer section.
        test_summary: Free-form test plan text for the ``## Test
            plan`` section.

    Returns:
        The rendered Markdown body.

    Raises:
        FileNotFoundError: If ``template_path`` is set and doesn't
            exist.
    """
    path = template_path or DEFAULT_TEMPLATE_PATH
    template = path.read_text(encoding="utf-8")

    # Build the per-line substitutions. Empty lists render as
    # ``_none_`` (the template placeholder, not literal) so the
    # template always substitutes successfully.
    stack_line = _render_stack_line(
        slice_index=slice_index, slice_total=slice_total,
        stack_id=stack_id,
    )
    issue_lines = _render_issue_lines(issue_numbers)
    reviewer_lines = _render_reviewer_lines(codeowners_reviewers)

    return template.format(
        task=task or "(no description provided)",
        head_branch=head_branch,
        base_branch=base_branch,
        stack_line=stack_line,
        issue_lines=issue_lines,
        reviewer_lines=reviewer_lines,
        test_summary=test_summary or "_No test plan provided._",
    )


def _render_stack_line(
    *,
    slice_index: int | None,
    slice_total: int | None,
    stack_id: str | None,
) -> str:
    """Build the ``- Stack: 2/3 (id)`` line.

    Returns an empty string for non-stacked jobs so the template
    line collapses to a bare ``-`` (or we can drop it — but
    keeping the line makes the template simpler).
    """
    if slice_index is None or slice_total is None:
        return "- Stack: single PR"
    parts = [f"- Stack: step {slice_index + 1}/{slice_total}"]
    if stack_id:
        parts.append(f"(id `{stack_id}`)")
    return " ".join(parts)


def _render_issue_lines(issues: Iterable[int] | None) -> str:
    """Render issue numbers as ``- Closes #N`` / ``- Refs #N`` lines.

    First issue is ``Closes`` (presumes the task is fixing it);
    remaining issues are ``Refs``. The first-Closes convention is
    GitHub's standard (it auto-closes the issue on merge).
    """
    if issues is None:
        return "_No linked issues._"
    nums = sorted(set(issues))
    if not nums:
        return "_No linked issues._"
    first, rest = nums[0], nums[1:]
    lines = [f"- Closes #{first}"]
    for n in rest:
        lines.append(f"- Refs #{n}")
    return "\n".join(lines)


def _render_reviewer_lines(reviewers: Iterable[str] | None) -> str:
    """Render reviewer usernames as ``- @user1 @user2 ...``.

    Each username is prefixed with ``@`` if it doesn't already start
    with ``@`` or ``/`` (a GitHub team path like ``/org/team``).
    """
    if reviewers is None:
        return "_No reviewers suggested._"
    cleaned: list[str] = []
    for r in reviewers:
        r = r.strip()
        if not r:
            continue
        if r.startswith(("@", "/")):
            cleaned.append(r)
        else:
            cleaned.append(f"@{r}")
    if not cleaned:
        return "_No reviewers suggested._"
    return "- " + " ".join(cleaned)


# === CODEOWNERS (Phase 2.5) ===

#: Default path (relative to the repo root) for the GitHub
#: CODEOWNERS file. We follow GitHub's resolution order: this
#: default is the most common location. Operators can override
#: via :func:`parse_codeowners_for_diff`'s ``codeowners_path`` arg.
DEFAULT_CODEOWNERS_PATH: Path = Path(".github/CODEOWNERS")


def _parse_codeowners_text(text: str) -> list[tuple[str, list[str]]]:
    """Parse a CODEOWNERS file's text into ``(pattern, owners)`` rows.

    Skips blank lines and ``#`` comments. Each non-comment line
    is split into a glob pattern (first token) and one-or-more
    owner tokens (the rest). An owner token can be:

    - ``@user`` — a personal handle (e.g. ``@octocat``)
    - ``@org/team`` — a team handle
    - ``user@org`` — an email (treated as a plain string; we
      don't validate it)

    Returns:
        List of ``(pattern, owners)`` tuples in the order they
        appeared. The order matters for negation patterns
        (``!pattern``) — we honor "last match wins" semantics
        by NOT pre-deduping, and let the caller match in order.
    """
    rows: list[tuple[str, list[str]]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # CODEOWNERS allows inline comments via space, so split
        # on whitespace and take all tokens (the first is the
        # pattern; the rest are owners).
        parts = line.split()
        if len(parts) < 1:
            continue
        pattern = parts[0]
        owners_raw = parts[1:]
        owners: list[str] = []
        for tok in owners_raw:
            tok = tok.strip()
            if not tok:
                continue
            owners.append(tok)
        rows.append((pattern, owners))
    return rows


# Phase 3 v1.5.0 Step 1: ``_match_codeowners_pattern`` extracted to
# :mod:`harness.privacy.path_match` (single source of truth для glob-семантики).
# Re-export в начале модуля (``from harness.privacy.path_match import match_glob
# as _match_codeowners_pattern``). Любой caller из ``pr_templating.py`` теперь
# использует единую реализацию, идентичную для CODEOWNERS (Phase 2.5) и
# Privacy zones (Phase 3 v1.5.0).


def parse_codeowners_for_diff(
    repo: Path,
    diff_files: Iterable[str],
    *,
    codeowners_path: Path | None = None,
) -> list[str]:
    """Return suggested CODEOWNERS reviewers for a diff.

    Reads ``<repo>/.github/CODEOWNERS`` (or ``codeowners_path`` if
    supplied), parses it, and for each file in ``diff_files``
    collects the union of owners whose patterns match. Returns
    a sorted, deduplicated list of owner strings (``@user``,
    ``@org/team``, or email).

    Pure function aside from the file read. The file read is
    wrapped in a ``try/except OSError`` so a missing or
    unreadable CODEOWNERS file simply yields ``[]`` instead of
    failing the PR creation. The ``fnmatch`` work is
    O(files × patterns) which is fast (typical repos: < 100
    files × ~20 patterns = 2000 ``fnmatch`` calls, <5ms).

    Args:
        repo: The repo root (a :class:`pathlib.Path`). The
              CODEOWNERS file is read from
              ``<repo>/.github/CODEOWNERS`` by default.
        diff_files: Iterable of repo-relative POSIX paths (the
                    output of ``git diff --name-only <base>``).
                    May be empty.
        codeowners_path: Optional override. If supplied, it is
                         interpreted relative to ``repo`` (use
                         an absolute path to escape the repo).

    Returns:
        Sorted, deduplicated list of owner strings. Empty if
        no CODEOWNERS file exists or no patterns matched.

    Examples:
        >>> from pathlib import Path
        >>> p = parse_codeowners_for_diff(
        ...     Path("/tmp/repo"), ["src/a.py", "docs/b.md"],
        ... )
        []  # no CODEOWNERS
    """
    if codeowners_path is None:
        codeowners_path = DEFAULT_CODEOWNERS_PATH
    full = repo / codeowners_path
    try:
        text = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Missing file, permission denied, or non-UTF-8 — treat
        # as "no owners" rather than crashing the PR phase.
        return []
    rows = _parse_codeowners_text(text)
    if not rows:
        return []
    # GitHub applies CODEOWNERS patterns in REVERSE order
    # (last match wins for a given path), so we iterate the
    # list backwards to build a per-file "winning" set of owners.
    # For union semantics (which is what we want — "any of these
    # people is a fine reviewer") we just collect every owner
    # that matches at least one file, ignoring negation.
    owners: set[str] = set()
    for f in diff_files:
        if not f:
            continue
        # Normalize: strip leading "./", convert backslashes
        # (Windows worktrees) to forward slashes.
        f_norm = f.lstrip("./").replace("\\", "/")
        for pattern, owners_in_row in rows:
            if not owners_in_row:
                continue
            if _match_codeowners_pattern(f_norm, pattern):
                owners.update(owners_in_row)
    return sorted(owners)


__all__ = [
    "DEFAULT_CODEOWNERS_PATH",
    "DEFAULT_TEMPLATE_PATH",
    "extract_issue_numbers",
    "parse_codeowners_for_diff",
    "render_pr_body",
]
