"""Tests for harness.agents.pr_integration (Phase 2.2, Step 2).

Covers:
  - check_gh_available() happy path (mocked _gh returns 0)
  - check_gh_available() raises GHUnavailable when binary missing
  - check_gh_available() raises GHUnavailable on "not logged in"
  - create_pr() parses URL from stdout
  - create_pr() propagates GHUnavailable
  - get_pr_status() parses gh pr view --json output
  - wait_for_checks() returns immediately on success
  - wait_for_checks() returns early on failure / changes_requested
  - wait_for_checks() raises asyncio.TimeoutError after timeout_s
  - merge_pr() runs --squash --delete-branch for squash=True
  - merge_pr() runs --merge --delete-branch for squash=False
  - end-to-end happy path: create_pr -> wait_for_checks -> merge_pr
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from harness.agents.pr_integration import (
    GHUnavailable,
    PRCreateResult,
    PRMergeResult,
    PRStatus,
    _parse_pr_status,
    check_gh_available,
    create_pr,
    get_pr_status,
    merge_pr,
    wait_for_checks,
)


# Set a fake GITHUB_TOKEN so tests that DON'T explicitly test the
# "not authenticated" path don't trip the env-var check in
# ``check_gh_available``. Tests that DO test the auth-failure path
# use ``monkeypatch.delenv("GITHUB_TOKEN", raising=False)`` to
# undo this.
@pytest.fixture(autouse=True)
def _fake_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-tests")


# === Stub helpers ===

def _register_call(
    stub_module: Any,
    gh_calls_ref: list[tuple[str, ...]],
    *,
    args_predicate: tuple[str, ...],
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Append a stubbed call to the queue. ``gh_calls_ref`` records what was called."""
    pass  # unused — we use gh_subprocess_stub fixture instead


# === check_gh_available ===

class TestCheckGHAvailable:
    async def test_happy_path_returns_none(
        self, gh_subprocess_stub, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``gh auth status`` returns 0, check_gh_available returns None."""
        gh_subprocess_stub([
            (("auth", "status"), 0, "Logged in to github.com\n", ""),
        ])
        result = await check_gh_available()
        assert result is None

    async def test_raises_when_binary_missing(
        self, gh_subprocess_stub, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``shutil.which("gh")`` is None, raise GHUnavailable."""
        from harness.agents import pr_integration
        monkeypatch.setattr(pr_integration.shutil, "which", lambda _: None)
        with pytest.raises(GHUnavailable, match="not found in PATH"):
            await check_gh_available()

    async def test_raises_on_not_logged_in(
        self, gh_subprocess_stub, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``gh auth status`` returns non-zero AND env var is empty, raise."""
        # Make sure the token env var is empty.
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        gh_subprocess_stub([
            (("auth", "status"), 1, "", "You are not logged into any GitHub hosts."),
        ])
        with pytest.raises(GHUnavailable, match="not authenticated"):
            await check_gh_available()

    async def test_raises_on_auth_failure_with_token(
        self, gh_subprocess_stub, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If env var is set but ``gh auth status`` still fails, raise with scope hint."""
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-test")
        gh_subprocess_stub([
            (("auth", "status"), 1, "", "HTTP 401: Bad credentials"),
        ])
        with pytest.raises(GHUnavailable, match="auth status failed"):
            await check_gh_available()


# === create_pr ===

class TestCreatePR:
    async def test_parses_url_from_stdout(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        """The last non-empty line of ``gh pr create`` stdout is the PR URL."""
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "create"), 0,
             "Creating pull request...\n"
             "https://github.com/owner/repo/pull/42\n", ""),
        ])
        result = await create_pr(
            repo=tmp_path, head_branch="harness/abc", base_branch="main",
            title="add widget", body="summary", draft=False,
        )
        assert isinstance(result, PRCreateResult)
        assert result.url == "https://github.com/owner/repo/pull/42"
        assert result.number == 42
        assert result.branch == "harness/abc"

    async def test_draft_flag_passed(
        self, gh_subprocess_stub, tmp_path: Path, monkeypatch,
    ) -> None:
        """``draft=True`` adds ``--draft`` to the command."""
        # Record the args the stub sees.
        from harness.agents import pr_integration
        seen: list[tuple[str, ...]] = []

        async def record_gh(*args, **kwargs):
            seen.append(tuple(args))
            # First call: auth status (return ok). Second: pr create.
            if "auth" in args:
                return (0, "", "")
            if "create" in args:
                return (0, "https://github.com/o/r/pull/7\n", "")
            return (1, "", "")

        monkeypatch.setattr(pr_integration, "_gh", record_gh)
        await create_pr(
            repo=tmp_path, head_branch="h/x", base_branch="main",
            title="t", body="b", draft=True,
        )
        # The third positional arg in ``pr create`` should include --draft.
        create_call = next(s for s in seen if "create" in s)
        assert "--draft" in create_call

    async def test_propagates_gh_unavailable(
        self, gh_subprocess_stub, tmp_path: Path, monkeypatch,
    ) -> None:
        """If check_gh_available raises, create_pr propagates."""
        from harness.agents import pr_integration
        monkeypatch.setattr(pr_integration.shutil, "which", lambda _: None)
        with pytest.raises(GHUnavailable):
            await create_pr(
                repo=tmp_path, head_branch="x", base_branch="main",
                title="t", body="b", draft=False,
            )

    async def test_raises_on_non_zero_exit(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        """Non-zero exit from ``gh pr create`` raises RuntimeError."""
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "create"), 1, "",
             "pull request already exists for this branch"),
        ])
        with pytest.raises(RuntimeError, match="gh pr create failed"):
            await create_pr(
                repo=tmp_path, head_branch="x", base_branch="main",
                title="t", body="b", draft=False,
            )


# === get_pr_status / _parse_pr_status ===

class TestGetPRStatus:
    async def test_parses_full_json(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        """A complete ``gh pr view --json`` payload parses into PRStatus."""
        payload = {
            "state": "OPEN",
            "merged": False,
            "statusCheckRollup": [
                {"state": "SUCCESS", "conclusion": "success"},
                {"state": "SUCCESS", "conclusion": "success"},
            ],
            "reviewDecision": "APPROVED",
        }
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "view"), 0, json.dumps(payload), ""),
        ])
        status = await get_pr_status(repo=tmp_path, pr_number=42)
        assert status.state == "open"
        assert status.merged is False
        assert status.checks_state == "success"
        assert status.review_decision == "approved"

    def test_parse_pr_status_reduces_checks(self) -> None:
        """``_reduce_checks`` collapses a list to a single enum value."""
        # All success.
        s = _parse_pr_status({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "SUCCESS", "conclusion": "success"},
            ],
            "reviewDecision": "",
        })
        assert s.checks_state == "success"

        # One failure => failure.
        s = _parse_pr_status({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "SUCCESS", "conclusion": "success"},
                {"state": "COMPLETED", "conclusion": "failure"},
            ],
            "reviewDecision": "",
        })
        assert s.checks_state == "failure"

        # One pending => pending.
        s = _parse_pr_status({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "SUCCESS", "conclusion": "success"},
                {"state": "PENDING", "conclusion": None},
            ],
            "reviewDecision": "",
        })
        assert s.checks_state == "pending"

        # Empty list => none.
        s = _parse_pr_status({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [],
            "reviewDecision": "",
        })
        assert s.checks_state == "none"

    def test_parse_pr_status_normalises_review(self) -> None:
        """ReviewDecision is normalised to lowercase."""
        s = _parse_pr_status({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [],
            "reviewDecision": "CHANGES_REQUESTED",
        })
        assert s.review_decision == "changes_requested"

    async def test_raises_on_invalid_json(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "view"), 0, "not json {", ""),
        ])
        with pytest.raises(RuntimeError, match="invalid JSON"):
            await get_pr_status(repo=tmp_path, pr_number=1)


# === wait_for_checks ===

class TestWaitForChecks:
    async def test_returns_immediately_on_success(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        """If the first poll already shows success+approved, return without sleeping."""
        payload = json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [{"state": "SUCCESS", "conclusion": "success"}],
            "reviewDecision": "APPROVED",
        })
        # We need multiple stub entries if wait_for_checks loops, but
        # the first call should be enough.
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "view"), 0, payload, ""),
        ])
        status = await wait_for_checks(
            repo=tmp_path, pr_number=42, poll_s=0.01, timeout_s=5.0,
        )
        assert status.checks_state == "success"
        assert status.review_decision == "approved"

    async def test_returns_early_on_changes_requested(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        """A reviewer rejection is a terminal return value, not a timeout."""
        payload = json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [{"state": "SUCCESS", "conclusion": "success"}],
            "reviewDecision": "CHANGES_REQUESTED",
        })
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "view"), 0, payload, ""),
        ])
        status = await wait_for_checks(
            repo=tmp_path, pr_number=42, poll_s=0.01, timeout_s=5.0,
        )
        assert status.review_decision == "changes_requested"

    async def test_returns_early_on_failure(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        payload = json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [
                {"state": "COMPLETED", "conclusion": "failure"},
            ],
            "reviewDecision": "",
        })
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "view"), 0, payload, ""),
        ])
        status = await wait_for_checks(
            repo=tmp_path, pr_number=42, poll_s=0.01, timeout_s=5.0,
        )
        assert status.checks_state == "failure"

    async def test_raises_timeout_on_persistent_pending(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        """A PR that's stuck in 'pending' past the timeout raises TimeoutError."""
        payload = json.dumps({
            "state": "OPEN", "merged": False,
            "statusCheckRollup": [{"state": "PENDING", "conclusion": None}],
            "reviewDecision": "",
        })
        # The stub is called many times by wait_for_checks. We register
        # the same response many times; the gh_subprocess_stub fixture
        # pops the first matching entry each call. For an infinite
        # poll, the last entry's pattern is what matters.
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            # We need enough ``pr view`` responses for all polls; the
            # fixture matches on prefix, so any number of these will
            # match the first call's predicate. We register 10 to be
            # safe; the test will timeout before consuming them all.
            (("pr", "view"), 0, payload, ""),
        ] * 10)
        with pytest.raises(asyncio.TimeoutError):
            await wait_for_checks(
                repo=tmp_path, pr_number=42, poll_s=0.001, timeout_s=0.05,
            )


# === merge_pr ===

class TestMergePR:
    async def test_squash_runs_squash_flag(
        self, gh_subprocess_stub, tmp_path: Path, monkeypatch,
    ) -> None:
        from harness.agents import pr_integration
        seen: list[tuple[str, ...]] = []

        async def record_gh(*args, **kwargs):
            seen.append(tuple(args))
            if "auth" in args:
                return (0, "", "")
            if "merge" in args:
                return (0, "abc1234 Merge commit\n", "")
            return (1, "", "")

        monkeypatch.setattr(pr_integration, "_gh", record_gh)
        result = await merge_pr(
            repo=tmp_path, pr_number=42, squash=True, delete_branch=True,
        )
        merge_call = next(s for s in seen if "merge" in s)
        assert "--squash" in merge_call
        assert "--delete-branch" in merge_call
        assert result.merged is True
        assert result.method == "squash"
        assert result.sha == "abc1234"

    async def test_non_squash_runs_merge_flag(
        self, gh_subprocess_stub, tmp_path: Path, monkeypatch,
    ) -> None:
        from harness.agents import pr_integration
        seen: list[tuple[str, ...]] = []

        async def record_gh(*args, **kwargs):
            seen.append(tuple(args))
            if "auth" in args:
                return (0, "", "")
            if "merge" in args:
                return (0, "def5678 Merge commit\n", "")
            return (1, "", "")

        monkeypatch.setattr(pr_integration, "_gh", record_gh)
        result = await merge_pr(
            repo=tmp_path, pr_number=42, squash=False, delete_branch=True,
        )
        merge_call = next(s for s in seen if "merge" in s)
        assert "--merge" in merge_call
        assert "--squash" not in merge_call
        assert result.method == "merge"

    async def test_raises_on_non_zero_exit(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        gh_subprocess_stub([
            (("auth", "status"), 0, "", ""),
            (("pr", "merge"), 1, "", "Pull Request is not mergeable"),
        ])
        with pytest.raises(RuntimeError, match="not mergeable"):
            await merge_pr(
                repo=tmp_path, pr_number=42, squash=True, delete_branch=True,
            )


# === End-to-end happy path ===

class TestEndToEnd:
    async def test_create_wait_merge(
        self, gh_subprocess_stub, tmp_path: Path,
    ) -> None:
        """Stub a full create -> wait -> merge sequence and assert the types."""
        gh_subprocess_stub([
            # create_pr: check_gh_available + gh pr create
            (("auth", "status"), 0, "", ""),
            (("pr", "create"), 0,
             "https://github.com/owner/repo/pull/99\n", ""),
            # wait_for_checks: check_gh_available + gh pr view
            (("auth", "status"), 0, "", ""),
            (("pr", "view"), 0, json.dumps({
                "state": "OPEN", "merged": False,
                "statusCheckRollup": [
                    {"state": "SUCCESS", "conclusion": "success"},
                ],
                "reviewDecision": "APPROVED",
            }), ""),
            # merge_pr: check_gh_available + gh pr merge
            (("auth", "status"), 0, "", ""),
            (("pr", "merge"), 0, "fedcba9\n", ""),
        ])
        created = await create_pr(
            repo=tmp_path, head_branch="harness/x", base_branch="main",
            title="t", body="b", draft=False,
        )
        assert created.number == 99

        status = await wait_for_checks(
            repo=tmp_path, pr_number=created.number, poll_s=0.01, timeout_s=5.0,
        )
        assert status.checks_state == "success"
        assert status.review_decision == "approved"

        merged = await merge_pr(
            repo=tmp_path, pr_number=created.number,
            squash=True, delete_branch=True,
        )
        assert merged.merged is True
        assert merged.sha == "fedcba9"
