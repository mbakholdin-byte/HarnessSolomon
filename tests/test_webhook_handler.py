"""Tests for the Phase 2.3 ``WebhookHandler`` (HMAC + parsing + dispatch).

Covers:
  - ``verify_github_signature`` — happy path, missing header,
    missing secret, bad HMAC
  - ``parse_github_payload`` — 3 event types (pull_request,
    check_run, pull_request_review) + unknown
  - ``WebhookHandler.handle_raw`` — full pipeline (verify +
    idempotency + parse + record) on happy path and redelivery
  - ``WebhookHandler.dispatch_event`` — pull_request
    closed+merged, check_run failure, review changes_requested,
    no-op cases
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from harness.agents.jobs import JobStore
from harness.agents.webhook_handler import (
    WebhookEvent,
    WebhookHandler,
    WebhookVerificationError,
    parse_github_payload,
    verify_github_signature,
)
from harness.agents.webhook_store import WebhookEventStore


# === Helpers ===

SECRET = "test-secret-32-chars-long-enough-for-hmac"


def _sign(body: bytes, secret: str = SECRET) -> str:
    """Return the ``X-Hub-Signature-256`` header value for ``body``."""
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


# === verify_github_signature ===

class TestVerifySignature:
    def test_happy_path(self) -> None:
        body = b'{"action":"closed"}'
        sig = _sign(body)
        # No exception = success.
        verify_github_signature(body=body, signature_header=sig, secret=SECRET)

    def test_bad_signature_raises(self) -> None:
        body = b'{"action":"closed"}'
        with pytest.raises(WebhookVerificationError) as exc_info:
            verify_github_signature(
                body=body,
                signature_header="sha256=deadbeef" + "0" * 56,
                secret=SECRET,
            )
        assert exc_info.value.reason == "bad_signature"

    def test_missing_signature_raises(self) -> None:
        with pytest.raises(WebhookVerificationError) as exc_info:
            verify_github_signature(
                body=b"{}", signature_header=None, secret=SECRET,
            )
        assert exc_info.value.reason == "missing_signature"

    def test_empty_signature_raises(self) -> None:
        with pytest.raises(WebhookVerificationError) as exc_info:
            verify_github_signature(
                body=b"{}", signature_header="", secret=SECRET,
            )
        assert exc_info.value.reason == "missing_signature"

    def test_empty_secret_raises(self) -> None:
        with pytest.raises(WebhookVerificationError) as exc_info:
            verify_github_signature(
                body=b"{}", signature_header="sha256=abc", secret="",
            )
        assert exc_info.value.reason == "missing_secret"

    def test_uppercase_scheme_accepted(self) -> None:
        """GitHub sends ``sha256=...`` lowercase. Accept ``SHA256=...`` too."""
        body = b'{"a":1}'
        sig = _sign(body).replace("sha256=", "SHA256=")
        verify_github_signature(body=body, signature_header=sig, secret=SECRET)

    def test_bad_signature_reason(self) -> None:
        """The reason attribute is set on every error path."""
        try:
            verify_github_signature(
                body=b"{}", signature_header="sha256=00", secret=SECRET,
            )
        except WebhookVerificationError as e:
            assert e.reason == "bad_signature"
        else:
            pytest.fail("expected WebhookVerificationError")


# === parse_github_payload ===

class TestParsePayload:
    def test_pull_request_closed(self) -> None:
        ev = parse_github_payload("pull_request", {
            "action": "closed",
            "number": 42,
            "pull_request": {
                "html_url": "https://github.com/owner/repo/pull/42",
                "head": {"sha": "abc123"},
                "state": "closed",
                "merged": True,
            },
        })
        assert ev.event_type == "pull_request"
        assert ev.action == "closed"
        assert ev.pr_number == 42
        assert ev.pr_url == "https://github.com/owner/repo/pull/42"
        assert ev.head_sha == "abc123"
        assert ev.pr_merged is True

    def test_pull_request_opened_not_merged(self) -> None:
        ev = parse_github_payload("pull_request", {
            "action": "opened",
            "number": 7,
            "pull_request": {
                "html_url": "https://x", "head": {"sha": "x"}, "merged": False,
            },
        })
        assert ev.action == "opened"
        assert ev.pr_merged is False

    def test_check_run_success(self) -> None:
        ev = parse_github_payload("check_run", {
            "action": "completed",
            "check_run": {
                "head_sha": "deadbeef",
                "conclusion": "success",
                "pull_requests": [
                    {"number": 99, "html_url": "https://x/99"},
                ],
            },
        })
        assert ev.event_type == "check_run"
        assert ev.conclusion == "success"
        assert ev.head_sha == "deadbeef"
        assert ev.pr_number == 99
        assert ev.pr_url == "https://x/99"

    def test_check_run_failure(self) -> None:
        ev = parse_github_payload("check_run", {
            "action": "completed",
            "check_run": {
                "head_sha": "h",
                "conclusion": "failure",
                "pull_requests": [{"number": 1, "html_url": "x"}],
            },
        })
        assert ev.conclusion == "failure"
        assert ev.pr_number == 1

    def test_check_run_no_linked_prs(self) -> None:
        """A check_run with no linked PRs (rare) → pr_number is None."""
        ev = parse_github_payload("check_run", {
            "action": "completed",
            "check_run": {"conclusion": "success", "head_sha": "h"},
        })
        assert ev.conclusion == "success"
        assert ev.pr_number is None

    def test_pull_request_review_approved(self) -> None:
        ev = parse_github_payload("pull_request_review", {
            "action": "submitted",
            "review": {"state": "approved"},
            "pull_request": {"number": 5, "html_url": "https://x/5"},
        })
        assert ev.event_type == "pull_request_review"
        assert ev.review_state == "approved"
        assert ev.pr_number == 5

    def test_pull_request_review_changes_requested(self) -> None:
        ev = parse_github_payload("pull_request_review", {
            "action": "submitted",
            "review": {"state": "changes_requested"},
            "pull_request": {"number": 5, "html_url": "https://x/5"},
        })
        assert ev.review_state == "changes_requested"

    def test_unknown_event_type(self) -> None:
        """An event type we don't handle returns a minimal event."""
        ev = parse_github_payload("push", {"ref": "refs/heads/main"})
        assert ev.event_type == "push"
        assert ev.pr_number is None
        assert ev.action is None


# === handle_raw ===

class TestHandleRaw:
    async def test_happy_path_records_and_returns(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        handler = WebhookHandler(store, SECRET)
        body = json.dumps({
            "action": "closed",
            "number": 42,
            "pull_request": {
                "html_url": "https://x/42", "head": {"sha": "h"},
                "merged": True, "state": "closed",
            },
        }).encode("utf-8")
        sig = _sign(body)
        ev = await handler.handle_raw(
            body=body, signature=sig, event_type="pull_request",
            delivery_id="d-1",
        )
        assert ev is not None
        assert ev.delivery_id == "d-1"
        assert ev.event_type == "pull_request"
        assert ev.pr_number == 42
        assert ev.pr_merged is True
        # And the store has the row.
        rec = await store.get_event("d-1")
        assert rec is not None
        assert rec.event_type == "pull_request"
        assert rec.processed is False  # mark_processed is the dispatcher's job

    async def test_duplicate_delivery_returns_none(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        handler = WebhookHandler(store, SECRET)
        body = b'{"action":"closed","number":1,"pull_request":{"html_url":"x","head":{"sha":"h"},"merged":true}}'
        sig = _sign(body)
        # First call: new.
        ev1 = await handler.handle_raw(
            body=body, signature=sig, event_type="pull_request",
            delivery_id="dup-1",
        )
        assert ev1 is not None
        # Second call (redelivery): None, no re-parse.
        ev2 = await handler.handle_raw(
            body=body, signature=sig, event_type="pull_request",
            delivery_id="dup-1",
        )
        assert ev2 is None

    async def test_bad_signature_raises(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        handler = WebhookHandler(store, SECRET)
        with pytest.raises(WebhookVerificationError):
            await handler.handle_raw(
                body=b"{}", signature="sha256=00", event_type="push",
                delivery_id="bad-1",
            )

    async def test_malformed_json_raises(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        store = WebhookEventStore(isolated_settings["auth_db_path"])
        await store.init()
        handler = WebhookHandler(store, SECRET)
        body = b"{not valid json"
        sig = _sign(body)
        with pytest.raises(ValueError, match="not valid JSON"):
            await handler.handle_raw(
                body=body, signature=sig, event_type="push",
                delivery_id="bad-json-1",
            )


# === dispatch_event ===

class TestDispatchEvent:
    async def test_pull_request_closed_merged_marks_job_merged(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        # Create a job in pr_auto_merge_enabled status.
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-d", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open",
            pr_url="https://x/42", pr_number=42,
        )
        await job_store.update_status(jid, "pr_auto_merge_enabled")
        # Now dispatch a "closed+merged" event for the same PR.
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-d1", event_type="pull_request",
            action="closed", pr_number=42,
            pr_url="https://x/42", pr_merged=True,
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is True
        assert result["action"] == "marked_merged"
        # And the job is now merged.
        rec = await job_store.load(jid)
        assert rec.status == "merged"
        assert rec.finished_at is not None

    async def test_check_run_failure_marks_job_failed(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-cr", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/7", pr_number=7,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-cr1", event_type="check_run",
            action="completed", pr_number=7,
            conclusion="failure",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is True
        rec = await job_store.load(jid)
        assert rec.status == "failed"
        assert "CI failed" in (rec.error or "")

    async def test_review_changes_requested_marks_failed(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-rv", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/9", pr_number=9,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-rv1", event_type="pull_request_review",
            action="submitted", pr_number=9, review_state="changes_requested",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is True
        rec = await job_store.load(jid)
        assert rec.status == "failed"
        assert "review" in (rec.error or "").lower()

    async def test_no_op_for_unknown_pr_number(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A webhook for a PR the queue didn't open → no-op."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-x", event_type="pull_request", action="closed",
            pr_number=999, pr_merged=True,
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is False
        assert "no job" in result["reason"]

    async def test_no_op_for_already_terminal_job(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A webhook arriving for a job that's already merged → no-op."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-t", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/3", pr_number=3,
        )
        await job_store.update_status(jid, "merged", finished=True)
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-t1", event_type="pull_request", action="closed",
            pr_number=3, pr_merged=True,
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is False
        assert "terminal" in result["reason"]

    async def test_check_run_success_is_noop(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A check_run success is a no-op (polling loop picks it up)."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-s", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/4", pr_number=4,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-s1", event_type="check_run", action="completed",
            pr_number=4, conclusion="success",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is False
        # Job is still in pr_waiting_checks (unchanged).
        rec = await job_store.load(jid)
        assert rec.status == "pr_waiting_checks"

    async def test_review_approved_without_merger_is_noop(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """Phase 2.4: 'approved' triggers ``_on_review_approved``.
        With no ``merger`` injected, the function returns a no-op
        and the job stays in its current status. The default
        ``WebhookHandler(store, secret)`` constructor doesn't
        inject a merger — operators wire it at server startup."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-a", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/6", pr_number=6,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)  # no merger
        event = WebhookEvent(
            delivery_id="d-a1", event_type="pull_request_review",
            action="submitted", pr_number=6, review_state="approved",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is False
        rec = await job_store.load(jid)
        assert rec.status == "pr_waiting_checks"

    async def test_event_without_pr_number_is_noop(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """An event with no pr_number (unknown type, or no linked PR)."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(delivery_id="d-n1", event_type="push")
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is False
        assert "no pr_number" in result["reason"]


# === Phase 2.4: approved short-circuit + multi-PR fan-out ===

class TestParsePhase24:
    """Phase 2.4: pr_numbers populated from check_run.pull_requests[]."""

    def test_check_run_with_multiple_linked_prs(
        self,
    ) -> None:
        """A single check_run linked to 3 PRs populates
        pr_numbers=[1,2,3]."""
        payload = {
            "action": "completed",
            "check_run": {
                "head_sha": "abc",
                "conclusion": "success",
                "pull_requests": [
                    {"number": 1, "html_url": "https://x/1"},
                    {"number": 2, "html_url": "https://x/2"},
                    {"number": 3, "html_url": "https://x/3"},
                ],
            },
        }
        event = parse_github_payload("check_run", payload)
        assert event.pr_numbers == [1, 2, 3]
        # pr_number still defaults to first (back-compat).
        assert event.pr_number == 1
        assert event.conclusion == "success"

    def test_check_run_with_no_linked_prs(self) -> None:
        """Edge case: check_run with no linked PRs."""
        payload = {
            "action": "completed",
            "check_run": {"head_sha": "abc", "conclusion": "success"},
        }
        event = parse_github_payload("check_run", payload)
        assert event.pr_numbers == []
        assert event.pr_number is None

    def test_pull_request_populates_pr_numbers(self) -> None:
        """pull_request events always have a length-1 pr_numbers list."""
        payload = {
            "action": "closed",
            "number": 42,
            "pull_request": {"html_url": "https://x/42"},
        }
        event = parse_github_payload("pull_request", payload)
        assert event.pr_numbers == [42]
        assert event.pr_number == 42


class TestApprovedShortCircuit:
    """Phase 2.4: ``_on_review_approved`` triggers merge via
    injected callable. Closes the Phase 2.3 explicit no-op."""

    async def test_review_approved_with_merger_marks_merged(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """An 'approved' review calls the injected merger and
        marks the job ``merged``."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-am", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/77", pr_number=77,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()

        # Inject a fake merger that records its call args.
        called_with: list[dict[str, Any]] = []
        async def fake_merger(*, repo, pr_number, env_var="GITHUB_TOKEN"):
            called_with.append({"repo": repo, "pr_number": pr_number, "env_var": env_var})

        handler = WebhookHandler(event_store, SECRET, merger=fake_merger)
        event = WebhookEvent(
            delivery_id="d-am1", event_type="pull_request_review",
            action="submitted", pr_number=77, review_state="approved",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is True
        assert result["action"] == "merged_via_review"
        # Merger was called with the right pr_number.
        assert called_with == [{"repo": None, "pr_number": 77, "env_var": "GITHUB_TOKEN"}]
        # Job is merged.
        rec = await job_store.load(jid)
        assert rec.status == "merged"

    async def test_review_approved_with_auto_merger_enables_auto(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """``_on_review_approved`` calls ``auto_merger`` (instead
        of ``merger``) when ``job.auto_merge=True``. Status
        transitions to ``pr_auto_merge_enabled``."""
        # JobRecord doesn't carry auto_merge, but we simulate by
        # wrapping the job in a lightweight shim. The handler
        # uses ``getattr(job, "auto_merge", False)`` so any
        # shim/object works.
        from dataclasses import dataclass as _dc
        from harness.agents.jobs import JobStore

        @_dc
        class _JobShim:
            id: str
            worktree_id: str
            status: str
            cost: float = 0.0
            pr_url: str | None = None
            pr_number: int | None = None
            repo: str | None = None
            auto_merge: bool = True  # shim field

        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-aam", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/88", pr_number=88,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()

        # Record which callable was called.
        called: list[str] = []
        async def fake_merger(**kwargs):
            called.append("merger")
        async def fake_auto_merger(**kwargs):
            called.append("auto_merger")

        handler = WebhookHandler(
            event_store, SECRET,
            merger=fake_merger, auto_merger=fake_auto_merger,
        )
        # Build a job shim that mirrors the stored row + auto_merge=True
        # (the dispatcher reads the stored JobRecord; this test
        # demonstrates that _on_review_approved is callable with
        # a shimmed object that has ``auto_merge=True``).
        rec = await job_store.load(jid)
        shim = _JobShim(
            id=rec.id, worktree_id=rec.worktree_id,
            status=rec.status, cost=rec.cost,
            pr_url=rec.pr_url, pr_number=rec.pr_number,
            repo=rec.repo, auto_merge=True,
        )
        result = await handler._on_review_approved(shim, job_store)
        assert result["action"] == "auto_merge_enabled"
        assert called == ["auto_merger"]
        # DB row should be pr_auto_merge_enabled.
        rec2 = await job_store.load(jid)
        assert rec2.status == "pr_auto_merge_enabled"

    async def test_review_approved_merger_failure_marks_failed(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A merger that raises marks the job ``failed`` with the
        exception details."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-mf", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/55", pr_number=55,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()

        async def failing_merger(**kwargs):
            raise RuntimeError("gh pr merge conflict")

        handler = WebhookHandler(event_store, SECRET, merger=failing_merger)
        event = WebhookEvent(
            delivery_id="d-mf1", event_type="pull_request_review",
            action="submitted", pr_number=55, review_state="approved",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is True
        assert result["action"] == "marked_failed"
        rec = await job_store.load(jid)
        assert rec.status == "failed"
        assert "approved review" in (rec.error or "").lower()

    async def test_review_approved_terminal_job_is_noop(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A review event for a job already in a terminal status
        is a no-op (the dispatcher doesn't re-merge)."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-t", model="m", prompt="p", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "merged", finished=True,  # already merged
            pr_url="https://x/99", pr_number=99,
        )
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()

        called: list[int] = []
        async def fake_merger(**kwargs):
            called.append(kwargs["pr_number"])

        handler = WebhookHandler(event_store, SECRET, merger=fake_merger)
        event = WebhookEvent(
            delivery_id="d-t1", event_type="pull_request_review",
            action="submitted", pr_number=99, review_state="approved",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is False
        assert "terminal" in result["reason"].lower()
        assert called == []  # merger was NOT called


class TestMultiPRFanOut:
    """Phase 2.4: dispatch_event fans out to multiple PRs for
    events like ``check_run`` with multiple linked PRs."""

    async def test_check_run_fanout_to_three_jobs(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A check_run with pr_numbers=[1,2,3] fans out to each
        job in merge_jobs. The merged result includes
        ``dispatched_to`` with 3 entries."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        ids = []
        for n in (1, 2, 3):
            jid = await job_store.create(
                worktree_id=f"wt-{n}", model="m", prompt="p",
                status="queued", pr_mode="draft",
            )
            await job_store.update_status(
                jid, "pr_open", pr_url=f"https://x/{n}", pr_number=n,
            )
            await job_store.update_status(jid, "pr_waiting_checks")
            ids.append(jid)
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-fan1", event_type="check_run",
            action="completed", pr_numbers=[1, 2, 3],
            conclusion="failure",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is True
        # 3 jobs dispatched to.
        assert len(result["dispatched_to"]) == 3
        for r in result["dispatched_to"]:
            assert r["processed"] is True
            assert r["action"] == "marked_failed"
        # All 3 jobs are now failed.
        for jid in ids:
            rec = await job_store.load(jid)
            assert rec.status == "failed"

    async def test_check_run_fanout_partial_unknown(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """Some PRs match jobs, others don't. processed=True
        if at least one matched."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        jid = await job_store.create(
            worktree_id="wt-x", model="m", prompt="p",
            status="queued", pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open", pr_url="https://x/50", pr_number=50,
        )
        await job_store.update_status(jid, "pr_waiting_checks")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        event = WebhookEvent(
            delivery_id="d-mix1", event_type="check_run",
            action="completed", pr_numbers=[50, 999],  # 999 unknown
            conclusion="failure",
        )
        result = await handler.dispatch_event(event, job_store)
        assert result["processed"] is True
        # 1 dispatch, 1 no-op.
        assert len(result["dispatched_to"]) == 2
        assert result["dispatched_to"][0]["processed"] is True
        assert result["dispatched_to"][1]["processed"] is False
        # Job 50 is failed.
        rec = await job_store.load(jid)
        assert rec.status == "failed"


class TestStackParentPromotion:
    """Phase 2.4: when a stack's last child merges, the parent
    orchestrator row is promoted to ``merged``."""

    async def test_parent_promoted_when_last_child_merges(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A 2-child stack: child 0 merges first, no promotion
        yet. child 1 merges, all_stack_children_merged=True,
        parent promoted to 'merged'."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        # Orchestrator row
        orch = await job_store.create(
            worktree_id="wt-o", model="m", prompt="p", status="queued",
            pr_mode="draft", pr_stack_id="stack-X", stack_position=0,
            stack_size=2,
        )
        # Child 0
        c0 = await job_store.create(
            worktree_id="wt-c0", model="m", prompt="p", status="queued",
            pr_mode="draft", pr_stack_id="stack-X", stack_position=1,
            stack_size=2, pr_url="https://x/100", pr_number=100,
        )
        await job_store.update_status(c0, "pr_open", pr_url="https://x/100", pr_number=100)
        await job_store.update_status(c0, "pr_auto_merge_enabled")
        # Child 1
        c1 = await job_store.create(
            worktree_id="wt-c1", model="m", prompt="p", status="queued",
            pr_mode="draft", pr_stack_id="stack-X", stack_position=2,
            stack_size=2, depends_on_pr_number=100,
            pr_url="https://x/101", pr_number=101,
        )
        await job_store.update_status(c1, "pr_open", pr_url="https://x/101", pr_number=101)
        await job_store.update_status(c1, "pr_auto_merge_enabled")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)

        # First child closes + merges.
        event0 = WebhookEvent(
            delivery_id="d-pp1", event_type="pull_request",
            action="closed", pr_number=100,
            pr_url="https://x/100", pr_merged=True,
        )
        r0 = await handler.dispatch_event(event0, job_store)
        assert r0["processed"] is True
        assert r0["action"] == "marked_merged"
        # No parent promotion yet (c1 still in flight).
        assert "promoted_parent" not in r0
        rec_orch = await job_store.load(orch)
        assert rec_orch.status != "merged"

        # Second child closes + merges.
        event1 = WebhookEvent(
            delivery_id="d-pp2", event_type="pull_request",
            action="closed", pr_number=101,
            pr_url="https://x/101", pr_merged=True,
        )
        r1 = await handler.dispatch_event(event1, job_store)
        assert r1["processed"] is True
        # Parent promotion triggered.
        assert "promoted_parent" in r1
        assert r1["promoted_parent"]["stack_id"] == "stack-X"
        assert r1["promoted_parent"]["parent_job_id"] == orch
        # Orchestrator row is now 'merged'.
        rec_orch = await job_store.load(orch)
        assert rec_orch.status == "merged"
        assert rec_orch.finished_at is not None

    async def test_parent_not_promoted_when_child_only(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A 3-child stack where only 2 of 3 children are merged:
        parent stays in pr_open."""
        job_store = JobStore(isolated_settings["auth_db_path"])
        orch = await job_store.create(
            worktree_id="wt-o2", model="m", prompt="p", status="queued",
            pr_mode="draft", pr_stack_id="stack-Y", stack_position=0,
            stack_size=3,
        )
        await job_store.update_status(orch, "pr_open")  # orchestrator in flight
        for n in (200, 201, 202):
            c = await job_store.create(
                worktree_id=f"wt-{n}", model="m", prompt="p",
                status="queued", pr_mode="draft", pr_stack_id="stack-Y",
                stack_position=(n - 199), stack_size=3,
                pr_url=f"https://x/{n}", pr_number=n,
            )
            await job_store.update_status(c, "pr_open", pr_url=f"https://x/{n}", pr_number=n)
            await job_store.update_status(c, "pr_auto_merge_enabled")
        event_store = WebhookEventStore(isolated_settings["auth_db_path"])
        await event_store.init()
        handler = WebhookHandler(event_store, SECRET)
        # Merge 2 of 3 children.
        for n in (200, 201):
            ev = WebhookEvent(
                delivery_id=f"d-py{n}", event_type="pull_request",
                action="closed", pr_number=n,
                pr_url=f"https://x/{n}", pr_merged=True,
            )
            r = await handler.dispatch_event(ev, job_store)
            assert "promoted_parent" not in r
        # Parent still in 'pr_open' (children 1, 2 are merged but 3 isn't).
        rec_orch = await job_store.load(orch)
        assert rec_orch.status == "pr_open"
