"""Tests for the Phase 2.3 inbound GitHub webhook HTTP route.

Covers:
  - Valid HMAC + pull_request closed+merged → 200, job marked merged
  - Valid HMAC + check_run failure → 200, job marked failed
  - Valid HMAC + review changes_requested → 200, job marked failed
  - Invalid HMAC → 401
  - Missing signature header → 401
  - webhook_secret='' → 503
  - Duplicate delivery_id (redelivery) → 200, no re-processing
  - Unknown event type → 200, logged + ignored
  - Malformed JSON payload → 400
  - Body > max_payload_kb → 413
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.agents.jobs import JobStore
from harness.config import settings
from harness.server.app import create_app


SECRET = "test-secret-32-chars-long-enough-for-hmac"


def _sign(body: bytes, secret: str = SECRET) -> str:
    """Return the ``X-Hub-Signature-256`` header value for ``body``."""
    mac = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _make_client(
    isolated_settings: dict[str, Path],
    webhook_secret: str = SECRET,
) -> TestClient:
    """A TestClient with the given webhook_secret (overrides conftest).

    The caller MUST use ``with _make_client(...) as client:`` — that
    triggers the FastAPI lifespan handler, which initialises
    ``app.state.webhook_handler``. (Without the lifespan, the
    handler is None and the route returns 503.)
    """
    settings.webhook_secret = webhook_secret
    app = create_app()
    return TestClient(app)


def _make_pr_payload(
    pr_number: int = 42,
    action: str = "closed",
    merged: bool = True,
) -> dict[str, Any]:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "html_url": f"https://github.com/o/r/pull/{pr_number}",
            "head": {"sha": "abc123"},
            "state": "closed" if action == "closed" else "open",
            "merged": merged,
        },
    }


def _make_check_run_payload(
    pr_number: int = 42,
    conclusion: str = "failure",
) -> dict[str, Any]:
    return {
        "action": "completed",
        "check_run": {
            "head_sha": "abc",
            "conclusion": conclusion,
            "pull_requests": [
                {"number": pr_number, "html_url": f"https://x/{pr_number}"},
            ],
        },
    }


def _make_review_payload(
    pr_number: int = 42,
    state: str = "changes_requested",
) -> dict[str, Any]:
    return {
        "action": "submitted",
        "review": {"state": state},
        "pull_request": {
            "number": pr_number, "html_url": f"https://x/{pr_number}",
        },
    }


def _create_job_in_store(
    isolated_settings: dict[str, Path],
    *,
    pr_number: int,
    status: str = "pr_auto_merge_enabled",
) -> str:
    """Seed a job in the JobStore at the given status + pr_number.

    IMPORTANT: the FastAPI lifespan creates the ``JobStore`` at
    ``settings.db_path.parent / "agent-jobs.db"``. The
    ``isolated_settings["auth_db_path"]`` is a different file
    (where the Phase 1.6 token store lives). We mirror the
    lifespan's path here so the handler's ``find_job_by_pr_number``
    actually finds the seeded job.
    """
    import asyncio
    from harness.config import settings as _settings
    job_store = JobStore(_settings.db_path.parent / "agent-jobs.db")
    async def _go():
        jid = await job_store.create(
            worktree_id=f"wt-webhook-{pr_number}",
            model="m", prompt="x", status="queued",
            pr_mode="draft",
        )
        await job_store.update_status(
            jid, "pr_open",
            pr_url=f"https://x/{pr_number}", pr_number=pr_number,
        )
        await job_store.update_status(jid, status)
        return jid
    return asyncio.run(_go())


# === Happy paths ===

class TestWebhookHappyPath:
    def test_pull_request_closed_merged_marks_job_merged(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps(_make_pr_payload(42, "closed", True)).encode()
        with _make_client(isolated_settings) as client:
            # Seed the job INSIDE the lifespan block — the
            # lifespan's recover_running() runs at startup and
            # cancels any in-flight job that pre-exists. Seeding
            # after that ensures the job is in the in-flight
            # status we want for the test.
            _create_job_in_store(
                isolated_settings, pr_number=42,
                status="pr_auto_merge_enabled",
            )
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-pr-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["delivery_id"] == "d-pr-1"
        assert out["event_type"] == "pull_request"
        assert out["processed"] is True, f"detail: {out.get('detail')!r}"
        # And the job was actually updated.
        import asyncio
        async def _check():
            from harness.config import settings as _s
            store = JobStore(_s.db_path.parent / "agent-jobs.db")
            return await store.find_job_by_pr_number(42)
        rec = asyncio.run(_check())
        assert rec is not None
        assert rec.status == "merged"

    def test_check_run_failure_marks_job_failed(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps(_make_check_run_payload(7, "failure")).encode()
        with _make_client(isolated_settings) as client:
            _create_job_in_store(
                isolated_settings, pr_number=7, status="pr_waiting_checks",
            )
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "check_run",
                    "X-GitHub-Delivery": "d-cr-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["processed"] is True
        import asyncio
        async def _check():
            from harness.config import settings as _s
            store = JobStore(_s.db_path.parent / "agent-jobs.db")
            return await store.find_job_by_pr_number(7)
        rec = asyncio.run(_check())
        assert rec is not None
        assert rec.status == "failed"

    def test_review_changes_requested_marks_job_failed(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps(_make_review_payload(9, "changes_requested")).encode()
        with _make_client(isolated_settings) as client:
            _create_job_in_store(
                isolated_settings, pr_number=9, status="pr_waiting_checks",
            )
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request_review",
                    "X-GitHub-Delivery": "d-rv-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 200, res.text
        assert res.json()["processed"] is True
        import asyncio
        async def _check():
            from harness.config import settings as _s
            store = JobStore(_s.db_path.parent / "agent-jobs.db")
            return await store.find_job_by_pr_number(9)
        rec = asyncio.run(_check())
        assert rec is not None
        assert rec.status == "failed"


# === Auth failures ===

class TestWebhookAuthFailures:
    def test_invalid_hmac_returns_401(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps(_make_pr_payload(42, "closed", True)).encode()
        with _make_client(isolated_settings) as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": "sha256=" + "0" * 64,
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-bad-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 401
        assert "bad_signature" in res.json()["detail"]

    def test_missing_signature_returns_401(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps(_make_pr_payload(42, "closed", True)).encode()
        with _make_client(isolated_settings) as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    # No X-Hub-Signature-256 header
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-miss-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 401
        assert "missing_signature" in res.json()["detail"]

    def test_empty_secret_returns_503(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps(_make_pr_payload(42, "closed", True)).encode()
        with _make_client(isolated_settings, webhook_secret="") as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-503-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 503
        assert "not configured" in res.json()["detail"].lower()


# === Idempotency ===

class TestWebhookIdempotency:
    def test_duplicate_delivery_returns_200_no_reprocess(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps(_make_pr_payload(42, "closed", True)).encode()
        # First delivery: success. We use a single client (single
        # lifespan) so the WebhookEventStore's `webhook_events`
        # table persists between the two POSTs.
        with _make_client(isolated_settings) as client:
            # Seed the job AFTER the lifespan (see note in
            # test_pull_request_closed_merged_marks_job_merged).
            _create_job_in_store(
                isolated_settings, pr_number=42,
                status="pr_auto_merge_enabled",
            )
            res1 = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-dup-1",
                    "Content-Type": "application/json",
                },
            )
        assert res1.status_code == 200
        assert res1.json()["processed"] is True
        # Redelivery: 200 + processed: false. New lifespan, but
        # the ``webhook_events`` table persists on disk so the
        # duplicate ``delivery_id`` is still detected.
        with _make_client(isolated_settings) as client:
            res2 = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-dup-1",
                    "Content-Type": "application/json",
                },
            )
        assert res2.status_code == 200
        assert res2.json()["processed"] is False
        assert "duplicate" in res2.json()["detail"]


# === Edge cases ===

class TestWebhookEdgeCases:
    def test_unknown_event_type_returns_200(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        with _make_client(isolated_settings) as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "push",  # not in our 3 handled types
                    "X-GitHub-Delivery": "d-push-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 200
        out = res.json()
        assert out["event_type"] == "push"
        # Processed should be False — we recorded but didn't dispatch.
        assert out["processed"] is False

    def test_malformed_json_returns_400(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = b"{not valid json"
        with _make_client(isolated_settings) as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-bad-json-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 400
        assert "not valid JSON" in res.json()["detail"]

    def test_missing_required_headers_returns_400(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        body = b'{}'
        with _make_client(isolated_settings) as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    # Missing X-GitHub-Event and X-GitHub-Delivery
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 400
        assert "missing required" in res.json()["detail"].lower()

    def test_body_too_large_returns_413(
        self, isolated_settings: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Lower the cap for the test (default 256KB → 1KB here).
        monkeypatch.setattr(settings, "webhook_max_payload_kb", 1)
        # Body > 1KB.
        big_body = b'{"action":"closed","data":"' + b"x" * 2000 + b'"}'
        with _make_client(isolated_settings) as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=big_body,
                headers={
                    "X-Hub-Signature-256": _sign(big_body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-big-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 413
        assert "too large" in res.json()["detail"]

    def test_webhook_for_unknown_pr_is_noop(
        self, isolated_settings: dict[str, Path],
    ) -> None:
        """A webhook for a PR the queue didn't open → 200 + processed: false."""
        body = json.dumps(_make_pr_payload(999, "closed", True)).encode()
        with _make_client(isolated_settings) as client:
            res = client.post(
                "/api/v1/agents/webhooks/github",
                content=body,
                headers={
                    "X-Hub-Signature-256": _sign(body),
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "d-orphan-1",
                    "Content-Type": "application/json",
                },
            )
        assert res.status_code == 200
        out = res.json()
        assert out["processed"] is False
        assert "no job" in (out.get("detail") or "")
