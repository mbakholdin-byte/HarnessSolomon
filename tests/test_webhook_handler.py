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

    async def test_review_approved_is_noop(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """Review 'approved' is a no-op in Phase 2.3 (Phase 2.4 review flow)."""
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
        handler = WebhookHandler(event_store, SECRET)
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
