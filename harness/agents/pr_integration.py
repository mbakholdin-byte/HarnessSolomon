"""GitHub PR integration via ``gh`` CLI (Phase 2.2, Step 2).

This module is the only place in the harness that talks to GitHub.
It is intentionally thin: each public function is a thin wrapper
over a single ``gh`` CLI invocation, with parsing of the response
into a typed Pydantic model.

Why ``gh`` instead of ``PyGithub``? Two reasons:
  1. **Zero new dependencies.** ``gh`` is already on the host
     (verified at planning time: ``gh version 2.88.1``); adding
     ``PyGithub`` would mean another runtime dep just for this.
  2. **Auth follows the user's env.** ``gh`` reads
     ``$GITHUB_TOKEN`` / ``$GH_TOKEN`` / ``gh auth login`` — the
     same auth surface that other GitHub tooling on the box uses.
     We don't have to ship our own OAuth dance.

Why a module-level ``_gh`` injection point? Tests monkeypatch
``_gh`` to fake the CLI without spawning a real subprocess. The
default implementation spawns the real binary via
``asyncio.create_subprocess_exec``. This is the same pattern we
use in :mod:`harness.agents.merge_queue` for ``_ff_merge`` and in
:mod:`harness.agents.worktree` for the git worktree operations.

Public API
----------

- :class:`GHUnavailable` — raised when ``gh`` isn't installed or
  isn't authenticated. ``.hint`` carries the next action.
- :class:`PRCreateResult` / :class:`PRStatus` / :class:`PRMergeResult`
  — Pydantic models for the data we care about.
- :func:`check_gh_available` — gate every PR operation.
- :func:`create_pr` — open a draft or ready-for-review PR.
- :func:`get_pr_status` — current state of a PR.
- :func:`wait_for_checks` — poll until CI checks + review reach
  a terminal state (success / failure / approved / changes_requested).
- :func:`merge_pr` — squash-merge or merge-commit a PR.

Note: this module imports only stdlib + Pydantic. It does NOT
import from :mod:`harness.agents.merge_queue` (so the trust
boundary from Phase 2.0 is preserved: ``harness.agents.*`` does
not import from ``harness.server.*`` or vice versa for the new
modules).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# === Errors ===

class GHUnavailable(RuntimeError):
    """Raised when ``gh`` is missing or not authenticated.

    The ``.hint`` attribute is a short, human-readable next step
    suitable for logging or surfacing in the CLI / Web UI.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint: str = hint


# === Pydantic models ===

class PRCreateResult(BaseModel):
    """Result of a successful ``gh pr create`` call."""

    url: str                   # e.g. "https://github.com/owner/repo/pull/12"
    number: int                # 12
    branch: str                # the head branch we just pushed


class PRStatus(BaseModel):
    """Snapshot of a PR's current state, parsed from ``gh pr view --json``."""

    state: Literal["open", "merged", "closed"] = "open"
    merged: bool = False
    #: GH returns ``statusCheckRollup`` as a list of check objects;
    #: we reduce to one of these strings. ``"none"`` means no checks
    #: configured (e.g. a repo without ``.github/workflows/``).
    checks_state: Literal[
        "pending", "success", "failure", "neutral", "skipped", "none",
    ] = "none"
    #: ``"none"`` for repos without CODEOWNERS / required-reviews.
    review_decision: Literal[
        "approved", "changes_requested", "review_required", "none",
    ] = "none"


class PRMergeResult(BaseModel):
    """Result of a successful ``gh pr merge`` call."""

    merged: bool = True
    method: Literal["merge", "squash", "rebase"] = "merge"
    sha: str | None = None      # the merge commit SHA, if GH printed it


# === Module-level injection point ===

async def _gh(*args: str, **kwargs: Any) -> tuple[int, str, str]:
    """Spawn a real ``gh`` subprocess and return ``(returncode, stdout, stderr)``.

    This is the default implementation behind the public API.
    Tests monkeypatch this symbol to fake the CLI without spawning
    a real subprocess. The signature mirrors what
    :func:`asyncio.create_subprocess_exec` returns, so test stubs
    are trivial to write.

    Args:
        *args:    Positional args after the ``gh`` binary. E.g.
                  ``_gh("pr", "create", "--base", "main", ...)``.
        **kwargs: Forwarded to :func:`asyncio.create_subprocess_exec`.
                  The most useful is ``env=`` (override env vars,
                  e.g. to inject ``$GITHUB_TOKEN``).

    Returns:
        Tuple of ``(returncode, stdout, stderr)``. Both stdout and
        stderr are decoded as UTF-8 with ``errors='replace'``.

    Raises:
        FileNotFoundError: ``gh`` is not on PATH.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    stdout_b, stderr_b = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


# === Public API ===

async def check_gh_available(*, env_var: str = "GITHUB_TOKEN") -> None:
    """Raise :class:`GHUnavailable` if ``gh`` is missing or unauthenticated.

    Called at the start of every PR operation. We check two things:

    1. ``shutil.which("gh")`` — fails if the binary isn't on PATH.
    2. ``gh auth status`` — fails if the user hasn't run
       ``gh auth login`` (or set the token env var). The token's
       *value* is NEVER read or logged; only its env-var *name*.

    The function returns ``None`` on success. It does not raise on
    transient network errors — those are caught by the caller when
    it actually tries to use the auth.
    """
    if shutil.which("gh") is None:
        raise GHUnavailable(
            "gh CLI not found in PATH",
            hint="Install from https://cli.github.com/ or via 'winget install GitHub.cli'",
        )
    rc, _stdout, stderr = await _gh("auth", "status")
    if rc != 0:
        # ``gh auth status`` returns non-zero with a friendly message
        # when not logged in. We check the env var explicitly because
        # some users prefer token-based auth over ``gh auth login``.
        if not os.environ.get(env_var, "").strip():
            raise GHUnavailable(
                f"gh is installed but not authenticated (env var {env_var} is empty)",
                hint=f"Run 'gh auth login' or set ${env_var} to a token with 'repo' scope",
            )
        # Token IS set in env, but ``gh auth status`` failed. The
        # most common cause is an expired token or wrong scope.
        raise GHUnavailable(
            f"gh auth status failed: {stderr.strip() or 'unknown'}",
            hint=f"Verify ${env_var} has 'repo' scope, or run 'gh auth login' to refresh",
        )
    return None


def _env_for_token(env_var: str) -> dict[str, str]:
    """Build a subprocess env with the GitHub token in ``GH_TOKEN``.

    ``gh`` reads ``GH_TOKEN`` (and ``GITHUB_TOKEN``) from the
    environment; we copy the user's value into the subprocess's
    env so it doesn't have to inherit the full parent env (which
    might not include the token if the user set it post-launch).
    """
    token = os.environ.get(env_var, "").strip()
    if not token:
        return {}
    return {"GH_TOKEN": token, "GITHUB_TOKEN": token}


#: Regular expression to extract a PR number from a GH URL.
#: Matches ``https://github.com/owner/repo/pull/123`` and any
#: trailing path/anchor/query string.
_PR_URL_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/pull/(\d+)")


async def create_pr(
    *,
    repo: Path,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    draft: bool,
    env_var: str = "GITHUB_TOKEN",
    body_file: Path | None = None,
) -> PRCreateResult:
    """Open a draft or ready-for-review PR via ``gh pr create``.

    Args:
        repo:        Absolute path to the local repo (used as ``cwd``).
        head_branch: The branch we just pushed (the sub-agent's branch).
        base_branch: The branch the PR targets (usually ``"main"``).
        title:       PR title.
        body:        PR body (Markdown). Ignored if ``body_file`` is
                     provided.
        draft:       If True, opens as a draft PR.
        env_var:     Name of the env var carrying the GitHub token.
        body_file:   Phase 2.4: if set, pass ``--body-file <path>``
                     to ``gh pr create`` instead of ``--body``. Useful
                     for long templated bodies that exceed
                     ``ARG_MAX`` (Windows: 32KB, Linux: 2MB). The
                     caller is responsible for cleanup; we do not
                     delete the file here.

    Returns:
        :class:`PRCreateResult` with ``url``, ``number``, ``branch``.

    Raises:
        GHUnavailable: ``gh`` missing or unauthenticated.
        RuntimeError:  ``gh pr create`` returned non-zero.
    """
    await check_gh_available(env_var=env_var)
    cmd = [
        "pr", "create",
        "--base", base_branch,
        "--head", head_branch,
        "--title", title,
    ]
    if body_file is not None:
        cmd.extend(["--body-file", str(body_file)])
    else:
        cmd.extend(["--body", body])
    if draft:
        cmd.append("--draft")
    env = _env_for_token(env_var)
    rc, stdout, stderr = await _gh(*cmd, cwd=str(repo), env=env)
    if rc != 0:
        raise RuntimeError(
            f"gh pr create failed (rc={rc}): {stderr.strip() or stdout.strip()}"
        )
    # The last non-empty line of stdout is the PR URL.
    last_line = next(
        (line.strip() for line in reversed(stdout.splitlines()) if line.strip()),
        "",
    )
    match = _PR_URL_RE.search(last_line)
    if not match:
        raise RuntimeError(
            f"gh pr create returned no parseable PR URL in stdout: {stdout!r}"
        )
    pr_number = int(match.group(1))
    return PRCreateResult(url=last_line, number=pr_number, branch=head_branch)


async def get_pr_status(
    *,
    repo: Path,
    pr_number: int,
    env_var: str = "GITHUB_TOKEN",
) -> PRStatus:
    """Return a snapshot of a PR's state via ``gh pr view --json``."""
    await check_gh_available(env_var=env_var)
    env = _env_for_token(env_var)
    rc, stdout, stderr = await _gh(
        "pr", "view", str(pr_number),
        "--json", "state,merged,statusCheckRollup,reviewDecision",
        cwd=str(repo), env=env,
    )
    if rc != 0:
        raise RuntimeError(
            f"gh pr view {pr_number} failed (rc={rc}): {stderr.strip() or stdout.strip()}"
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"gh pr view {pr_number} returned invalid JSON: {e}\nstdout: {stdout!r}"
        ) from e
    return _parse_pr_status(data)


def _parse_pr_status(data: dict[str, Any]) -> PRStatus:
    """Parse the JSON from ``gh pr view --json`` into a :class:`PRStatus`.

    Tolerant of missing keys (older ``gh`` versions) and unusual
    shapes (e.g. an empty ``statusCheckRollup`` list when no
    checks are configured). Normalises GH's uppercase enum values
    (``OPEN``, ``APPROVED``, ...) to the lowercase form expected
    by the Pydantic Literal types in :class:`PRStatus`.
    """
    state_raw = (data.get("state") or "open").upper()
    state = {
        "OPEN": "open",
        "MERGED": "merged",
        "CLOSED": "closed",
    }.get(state_raw, "open")
    merged = bool(data.get("merged", False))
    # ``statusCheckRollup`` is either a list of {state, conclusion}
    # dicts OR a single object on older ``gh`` versions. We treat
    # both as a list.
    rollup = data.get("statusCheckRollup") or []
    if isinstance(rollup, dict):
        rollup = [rollup]
    checks_state = _reduce_checks(rollup)
    review_decision = (data.get("reviewDecision") or "").upper()
    return PRStatus(
        state=state,
        merged=merged,
        checks_state=checks_state,
        review_decision=_normalise_review(review_decision),
    )


def _reduce_checks(rollup: list[dict[str, Any]]) -> str:
    """Reduce a ``statusCheckRollup`` list to one of the :class:`PRStatus` enum values.

    Returns ``"none"`` for an empty list (repo with no CI).
    Returns ``"success"`` only if every check succeeded.
    Returns ``"failure"`` if any check has a failing conclusion.
    Returns ``"pending"`` if any check is in progress (no conclusion yet).
    Otherwise ``"neutral"`` (cancelled / skipped).
    """
    if not rollup:
        return "none"
    has_pending = False
    for check in rollup:
        state = (check.get("state") or "").upper()
        conclusion = (check.get("conclusion") or "").upper()
        if state == "PENDING" or state == "QUEUED" or state == "IN_PROGRESS":
            has_pending = True
            continue
        if conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            return "failure"
    return "pending" if has_pending else "success"


def _normalise_review(value: str) -> str:
    """Map GH's uppercase enum to the lowercase form in :class:`PRStatus`."""
    return {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "changes_requested",
        "REVIEW_REQUIRED": "review_required",
        "": "none",
    }.get(value, "none")


async def wait_for_checks(
    *,
    repo: Path,
    pr_number: int,
    poll_s: float = 15.0,
    timeout_s: float = 300.0,
    env_var: str = "GITHUB_TOKEN",
) -> PRStatus:
    """Poll the PR until CI checks and review decision reach a terminal state.

    Returns:
        :class:`PRStatus` when checks have reached ``success`` (and
        the review decision is either ``approved`` or ``none``).

    Returns early (does NOT raise) when:
      - ``checks_state == "failure"`` (CI red)
      - ``review_decision == "changes_requested"`` (reviewer rejected)
      - ``state in ("merged", "closed")`` (someone merged/closed it
        out-of-band)

    Raises:
        asyncio.TimeoutError: ``timeout_s`` elapsed with the PR
            still pending. The caller should mark the job ``failed``
            with a "PR checks timed out" error.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        status = await get_pr_status(
            repo=repo, pr_number=pr_number, env_var=env_var,
        )
        # Terminal (out-of-band) states.
        if status.state in ("merged", "closed") or status.merged:
            return status
        # Reviewer rejected.
        if status.review_decision == "changes_requested":
            return status
        # CI failed.
        if status.checks_state == "failure":
            return status
        # CI green + review either approved or absent → ready to merge.
        if status.checks_state == "success" and status.review_decision in (
            "approved", "none",
        ):
            return status
        # Otherwise keep waiting.
        if asyncio.get_event_loop().time() >= deadline:
            raise asyncio.TimeoutError(
                f"PR {pr_number} did not reach a mergeable state within {timeout_s}s"
            )
        await asyncio.sleep(poll_s)


async def merge_pr(
    *,
    repo: Path,
    pr_number: int,
    squash: bool = False,
    delete_branch: bool = True,
    env_var: str = "GITHUB_TOKEN",
) -> PRMergeResult:
    """Merge a PR via ``gh pr merge`` (squash or merge commit).

    Args:
        repo:           Local repo path (used as ``cwd``).
        pr_number:      PR number.
        squash:         If True, squash-merge (default commit).
        delete_branch:  If True, delete the head branch after merge.
        env_var:        GitHub token env var.

    Returns:
        :class:`PRMergeResult` with ``merged=True`` and the method used.

    Raises:
        GHUnavailable: ``gh`` missing or unauthenticated.
        RuntimeError:  ``gh pr merge`` failed (e.g. PR already merged,
            branch protection blocked, etc.).
    """
    await check_gh_available(env_var=env_var)
    cmd = ["pr", "merge", str(pr_number)]
    if squash:
        cmd.append("--squash")
    else:
        cmd.append("--merge")
    if delete_branch:
        cmd.append("--delete-branch")
    env = _env_for_token(env_var)
    rc, stdout, stderr = await _gh(*cmd, cwd=str(repo), env=env)
    if rc != 0:
        # Common cases:
        #   "Pull Request is not mergeable" — branch protection
        #   "already merged" — race with another actor
        # We surface a clean error and let the caller decide.
        raise RuntimeError(
            f"gh pr merge {pr_number} failed (rc={rc}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    # ``gh pr merge --delete-branch --squash`` prints the merge commit
    # SHA on stdout when successful (since gh 2.50+). We try to extract
    # it; if not present, return sha=None.
    sha: str | None = None
    sha_match = re.search(r"\b([0-9a-f]{7,40})\b", stdout)
    if sha_match:
        sha = sha_match.group(1)
    return PRMergeResult(merged=True, method="squash" if squash else "merge", sha=sha)


# === Phase 2.3: auto-merge (branch-protection-aware) ===

async def enable_auto_merge(
    *,
    repo: Path,
    pr_number: int,
    merge_method: Literal["squash", "merge", "rebase"] = "squash",
    delete_branch: bool = True,
    env_var: str = "GITHUB_TOKEN",
) -> None:
    """Enable GitHub's branch-protection-aware auto-merge.

    Calls ``gh pr merge <N> --auto --<method> [--delete-branch]``.
    Unlike :func:`merge_pr`, this does NOT block until the PR is
    merged — it returns as soon as GitHub accepts the request
    ("auto-merge enabled"). GitHub then waits for the branch-
    protection conditions (e.g. outstanding approvals) to clear
    and performs the actual merge in the background.

    The caller is expected to track the job's status separately
    (Phase 2.3: ``pr_auto_merge_enabled`` → ``merged`` via the
    inbound webhook in :mod:`harness.agents.webhook_handler`).

    Args:
        repo:           Local repo path (used as ``cwd``).
        pr_number:      PR number.
        merge_method:   ``"squash"`` (default), ``"merge"`` (merge
                        commit), or ``"rebase"`` (rebase + ff).
        delete_branch:  Whether to pass ``--delete-branch`` to clean
                        up the head branch after GitHub's auto-merge
                        completes. Default True.
        env_var:        GitHub token env var.

    Raises:
        GHUnavailable: ``gh`` missing or not authenticated.
        RuntimeError:  ``gh pr merge --auto`` returned non-zero. The
                       two common cases:
                       - "Pull Request is not mergeable" — branch
                         protection is misconfigured (no required
                         status checks / reviewers)
                       - "auto-merge is not allowed" — branch
                         protection has not enabled auto-merge for
                         this branch
                       The caller is expected to either surface
                       the error to the user OR fall back to a
                       direct :func:`merge_pr` (Phase 2.2 path).

    Note:
        The ``--auto`` flag was added to ``gh`` in v2.19.0. The
        harness assumes ``gh >= 2.50`` (the same baseline as
        :func:`merge_pr`).
    """
    await check_gh_available(env_var=env_var)
    cmd = [
        "pr", "merge", str(pr_number),
        "--auto",
        f"--{merge_method}",
    ]
    if delete_branch:
        cmd.append("--delete-branch")
    env = _env_for_token(env_var)
    rc, stdout, stderr = await _gh(*cmd, cwd=str(repo), env=env)
    if rc != 0:
        raise RuntimeError(
            f"gh pr merge {pr_number} --auto failed (rc={rc}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    return None


async def disable_auto_merge(
    *,
    repo: Path,
    pr_number: int,
    env_var: str = "GITHUB_TOKEN",
) -> None:
    """Cancel a previously enabled auto-merge.

    Calls ``gh pr merge <N> --disable-auto``. Useful when a job
    is cancelled mid-flight and we want to tell GitHub to stop
    waiting for branch-protection conditions.

    Args:
        repo:        Local repo path (used as ``cwd``).
        pr_number:   PR number.
        env_var:     GitHub token env var.

    Raises:
        GHUnavailable: ``gh`` missing or not authenticated.
        RuntimeError:  ``gh pr merge --disable-auto`` returned
                       non-zero (rare; usually means the PR
                       doesn't have auto-merge enabled in the
                       first place, which is fine to ignore).
    """
    await check_gh_available(env_var=env_var)
    cmd = ["pr", "merge", str(pr_number), "--disable-auto"]
    env = _env_for_token(env_var)
    rc, stdout, stderr = await _gh(*cmd, cwd=str(repo), env=env)
    if rc != 0:
        raise RuntimeError(
            f"gh pr merge {pr_number} --disable-auto failed (rc={rc}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    return None


__all__ = [
    "GHUnavailable",
    "PRCreateResult",
    "PRStatus",
    "PRMergeResult",
    "check_gh_available",
    "create_pr",
    "get_pr_status",
    "wait_for_checks",
    "merge_pr",
    # Phase 2.3
    "enable_auto_merge",
    "disable_auto_merge",
]
