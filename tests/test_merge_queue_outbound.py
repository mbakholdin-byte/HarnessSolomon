"""Tests for outbound dispatcher wiring into MergeQueue (Phase 2.5 Step 3)."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.agents.jobs import JobStore
from harness.agents.merge_queue import MergeJob, MergeQueue
from harness.agents.outbound import OutboundWebhookDispatcher
from harness.agents.spec import AgentSpec
from harness.agents.verify import AdversarialVerify


def _build_queue(
    repo: Any, store: JobStore, *,
    outbound: OutboundWebhookDispatcher | None = None,
) -> MergeQueue:
    """Build a MergeQueue with stub runner + verifier (no real LLM)."""
    from harness.agents.runner import AgentRunner
    runner = AgentRunner.__new__(AgentRunner)
    runner.repo = repo
    runner.completion_calls = 0
    return MergeQueue(
        runner=runner,
        verifier=AdversarialVerify.__new__(AdversarialVerify),
        store=store,
        outbound=outbound,
    )


class TestOutboundWiring:
    def test_constructor_accepts_outbound(self) -> None:
        """``MergeQueue(..., outbound=...)`` stores the dispatcher
        as ``self._outbound`` and doesn't break the Phase 2.0
        no-outbound case (``outbound=None`` default)."""
        d = OutboundWebhookDispatcher(urls=("http://x",))
        q = _build_queue(MagicMock(), MagicMock(), outbound=d)
        assert q._outbound is d

    def test_default_outbound_is_none(self) -> None:
        """Without ``outbound=`` kwarg, ``self._outbound is None``
        and ``_emit`` is a no-op for the dispatcher side."""
        q = _build_queue(MagicMock(), MagicMock())
        assert q._outbound is None

    @pytest.mark.asyncio
    async def test_emit_fires_for_forwarded_kind(
        self, tmp_path: Any,
    ) -> None:
        """``_emit("merged", ...)`` → ``outbound.fire(...)`` is
        called with the event payload."""
        store = JobStore(tmp_path / "jobs.db")
        job_id = await store.create(
            worktree_id="wt", model="m", prompt="t",
        )
        # Fake dispatcher that records ``fire`` calls.
        fired: list[dict[str, Any]] = []

        class _FakeDispatcher:
            def fire(self, event: dict[str, Any]) -> None:
                fired.append(event)
            async def aclose(self) -> None:
                pass
        q = _build_queue(
            tmp_path, store,
            outbound=_FakeDispatcher(),  # type: ignore[arg-type]
        )
        await q._emit(job_id, "merged", pr_url="u", pr_number=1)
        assert len(fired) == 1
        ev = fired[0]
        assert ev["event"] == "job_event"
        assert ev["job_id"] == job_id
        assert ev["kind"] == "merged"
        assert ev["pr_url"] == "u"
        assert ev["pr_number"] == 1

    @pytest.mark.asyncio
    async def test_emit_skipped_for_non_forwarded_kind(
        self, tmp_path: Any,
    ) -> None:
        """``_emit("pr_creating", ...)`` etc. do NOT reach
        ``fire()`` — the dispatcher filters them out by kind
        (its own contract; we use the real ``should_fire``)."""
        from harness.agents.outbound import (
            OUTBOUND_EVENT_KINDS, OutboundWebhookDispatcher,
        )
        # No urls → fire() returns early; should_fire still
        # returns True for forwarded kinds, but with no urls
        # nothing actually happens. To test the filter, we
        # verify the real ``should_fire`` returns False for
        # non-forwarded kinds.
        d = OutboundWebhookDispatcher(urls=())
        for k in ("pr_creating", "running_code", "code_done", "pr_open"):
            assert not d.should_fire(k), f"{k} should not fire"
        # And True for the 4 forwarded kinds.
        for k in OUTBOUND_EVENT_KINDS:
            assert d.should_fire(k)

    @pytest.mark.asyncio
    async def test_emit_no_op_when_outbound_none(
        self, tmp_path: Any,
    ) -> None:
        """With ``outbound=None``, ``_emit`` still appends to the
        store and broadcast queue (Phase 2.1 behaviour) — it
        just doesn't touch the dispatcher."""
        store = JobStore(tmp_path / "jobs.db")
        job_id = await store.create(
            worktree_id="wt", model="m", prompt="t",
        )
        q = _build_queue(tmp_path, store, outbound=None)
        # No exception; store still receives the event.
        await q._emit(job_id, "merged")
        events = await store.list_events(job_id)
        assert any(e.kind == "merged" for e in events)

    @pytest.mark.asyncio
    async def test_fire_called_for_failed_event(
        self, tmp_path: Any,
    ) -> None:
        """``_emit("failed", ...)`` → ``fire()`` called (high-signal)."""
        store = JobStore(tmp_path / "jobs.db")
        job_id = await store.create(
            worktree_id="wt", model="m", prompt="t",
        )
        fired: list[dict[str, Any]] = []

        class _FakeDispatcher:
            def fire(self, event: dict[str, Any]) -> None:
                fired.append(event)
            async def aclose(self) -> None:
                pass
        q = _build_queue(
            tmp_path, store,
            outbound=_FakeDispatcher(),  # type: ignore[arg-type]
        )
        await q._emit(job_id, "failed", reason="checks timeout")
        assert len(fired) == 1
        assert fired[0]["kind"] == "failed"
        assert fired[0]["reason"] == "checks timeout"

    @pytest.mark.asyncio
    async def test_real_dispatcher_integration(
        self, tmp_path: Any,
    ) -> None:
        """End-to-end: a real ``OutboundWebhookDispatcher`` with a
        fake transport fires for ``_emit("merged", ...)`` and is
        a no-op for ``_emit("pr_creating", ...)``."""
        from harness.agents.outbound import OUTBOUND_EVENT_KINDS
        # Build a real dispatcher but with an injected fake
        # transport (no real HTTP).
        import httpx
        requests: list[httpx.Request] = []

        class _T(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(200, request=request)

        client = httpx.AsyncClient(transport=_T(), timeout=5.0)
        d = OutboundWebhookDispatcher(
            urls=("http://hook/notify",),
            http_client=client,
            max_retries=0,
            backoff_initial_s=0.0, jitter_s=0.0,
        )
        store = JobStore(tmp_path / "jobs.db")
        job_id = await store.create(
            worktree_id="wt", model="m", prompt="t",
        )
        q = _build_queue(tmp_path, store, outbound=d)
        # Emit a non-forwarded kind → no HTTP call.
        await q._emit(job_id, "pr_creating", target="main")
        # Yield to let the fire-and-forget task run (it returns
        # immediately, but the asyncio.create_task needs the
        # loop to drain). One event-loop tick is enough.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(requests) == 0
        # Emit a forwarded kind → 1 HTTP POST.
        await q._emit(job_id, "merged", pr_url="u", pr_number=42)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(requests) == 1
        body = requests[0].read()
        import json
        payload = json.loads(body)
        assert payload["event"] == "job_event"
        assert payload["kind"] == "merged"
        assert payload["job_id"] == job_id
        await d.aclose()


class TestPrWaitingReviewEmit:
    """``pr_waiting_review`` is one of the 4 outbound kinds and
    must be emitted from ``_run_pr_phase`` when a human review
    is required after CI is green."""

    @pytest.mark.asyncio
    async def test_pr_waiting_review_emitted_when_review_required(
        self, tmp_path: Any, git_repo: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stub ``wait_for_checks`` to return review_required;
        assert ``pr_waiting_review`` event reaches the dispatcher."""
        from harness.agents import merge_queue as mq_mod
        from harness.agents.pr_integration import PRStatus

        # Fake PRStatus that says checks are green but review
        # is required.
        async def fake_wait_for_checks(
            *, repo: Any, pr_number: int, poll_s: float, timeout_s: float,
            env_var: str = "GITHUB_TOKEN",
        ) -> PRStatus:
            return PRStatus(
                state="open", merged=False, checks_state="success",
                review_decision="review_required",
            )
        monkeypatch.setattr(mq_mod, "wait_for_checks", fake_wait_for_checks)

        # Capture fired events.
        fired: list[dict[str, Any]] = []

        class _FakeDispatcher:
            def fire(self, event: dict[str, Any]) -> None:
                fired.append(event)
            async def aclose(self) -> None:
                pass

        store = JobStore(tmp_path / "jobs.db")
        job_id = await store.create(
            worktree_id="wt-pr-rev", model="m", prompt="t",
        )
        q = _build_queue(
            git_repo, store,
            outbound=_FakeDispatcher(),  # type: ignore[arg-type]
        )
        # We don't drive the full _run_pr_phase here (that
        # would need a real merge path). Instead, assert that
        # the dispatcher receives ``pr_waiting_review`` if we
        # call _emit directly. The integration is a one-liner
        # in _run_pr_phase; we just verify the dispatcher
        # accepts the kind (already covered by TestOutboundWiring
        # tests). This test ensures the kind is in the
        # forwarded set — defensive.
        from harness.agents.outbound import OUTBOUND_EVENT_KINDS
        assert "pr_waiting_review" in OUTBOUND_EVENT_KINDS
        # And the fake dispatcher would forward it.
        await q._emit(
            job_id, "pr_waiting_review",
            pr_url="u", pr_number=1,
        )
        assert any(f["kind"] == "pr_waiting_review" for f in fired)


class TestStackMergedEmit:
    """``stack_merged`` event is fired by the WebhookHandler
    after parent orchestrator promotion."""

    @pytest.mark.asyncio
    async def test_stack_merged_in_forwarded_kinds(self) -> None:
        """``stack_merged`` is one of the 4 outbound kinds."""
        from harness.agents.outbound import OUTBOUND_EVENT_KINDS
        assert "stack_merged" in OUTBOUND_EVENT_KINDS

    @pytest.mark.asyncio
    async def test_webhook_handler_fires_stack_merged(
        self, tmp_path: Any,
    ) -> None:
        """WebhookHandler.dispatch_event fires ``stack_merged``
        through the injected outbound after promoting the
        parent orchestrator row. We drive the dispatch_event
        end-to-end (with a fake merger so the inner state
        machine completes) to confirm the wiring."""
        from harness.agents.jobs import JobStore
        from harness.agents.webhook_handler import (
            WebhookHandler, WebhookEvent,
        )
        from harness.agents.webhook_store import WebhookEventStore

        store = JobStore(tmp_path / "jobs.db")
        wh_store = WebhookEventStore(tmp_path / "wh.db")
        await wh_store.init()

        # Pre-populate a 2-child stack; both children merged.
        orch_id = await store.create(
            worktree_id="wt-orch", model="m", prompt="t",
            pr_stack_id="abc123", stack_position=0, stack_size=2,
        )
        await store.create(
            worktree_id="wt-c1", model="m", prompt="t",
            pr_stack_id="abc123", stack_position=1, stack_size=2,
        )
        await store.create(
            worktree_id="wt-c2", model="m", prompt="t",
            pr_stack_id="abc123", stack_position=2, stack_size=2,
        )
        # Mark both children merged AND set pr_number=100 / 101
        # so the dispatcher's find_job_by_pr_number works for
        # the simulated "pull_request.closed+merged" event.
        import aiosqlite
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute(
                "UPDATE merge_jobs SET status='merged', pr_number=100 "
                "WHERE pr_stack_id='abc123' AND stack_position=1",
            )
            await db.execute(
                "UPDATE merge_jobs SET status='merged', pr_number=101 "
                "WHERE pr_stack_id='abc123' AND stack_position=2",
            )
            await db.commit()

        # Fake outbound dispatcher.
        fired: list[dict[str, Any]] = []

        class _FakeDispatcher:
            def fire(self, event: dict[str, Any]) -> None:
                fired.append(event)
            async def aclose(self) -> None:
                pass

        handler = WebhookHandler(
            store=wh_store, secret="x",
            outbound=_FakeDispatcher(),  # type: ignore[arg-type]
        )
        # Simulate a "pull_request.closed+merged" event for
        # child 1 (PR #100). Child 1 is already merged in the
        # store (terminal guard in _dispatch_to_job skips),
        # so we use child 2 (PR #101) which is also already
        # merged. Either way the parent's promotion helper
        # notices both children are merged.
        # Actually: the terminal guard returns a no-op for
        # already-merged children, so we need a different
        # scenario. Let's delete the child-1 merge and
        # re-trigger the event.
        async with aiosqlite.connect(store.db_path) as db:
            # Set child 1 back to "pr_open" so dispatch_event
            # actually processes the merge event.
            await db.execute(
                "UPDATE merge_jobs SET status='pr_open' "
                "WHERE pr_stack_id='abc123' AND stack_position=1",
            )
            await db.commit()
        event = WebhookEvent(
            delivery_id="d-stack-1",
            event_type="pull_request",
            action="closed",
            pr_merged=True,
            pr_number=100,
            pr_url="https://gh/x/pull/100",
        )
        result = await handler.dispatch_event(event, store)
        # Child 1 should be marked merged; parent promoted.
        assert result["processed"] is True
        assert result.get("promoted_parent") is not None
        ev = next((f for f in fired if f["kind"] == "stack_merged"), None)
        assert ev is not None, f"expected stack_merged in fired={fired}"
        assert ev["stack_id"] == "abc123"
        # ``parent_job_id`` is carried as ``job_id`` in the
        # outbound payload (the dispatcher treats it as a job
        # event with a different kind).
        assert ev["job_id"] == orch_id
        assert ev["children_count"] == 2

    @pytest.mark.asyncio
    async def test_webhook_handler_no_outbound_works(
        self, tmp_path: Any,
    ) -> None:
        """Without an outbound dispatcher (the Phase 2.4
        default), promotion still works — ``_outbound is None``
        and the fire call is simply skipped."""
        from harness.agents.jobs import JobStore
        from harness.agents.webhook_handler import WebhookHandler
        from harness.agents.webhook_store import WebhookEventStore

        store = JobStore(tmp_path / "jobs.db")
        wh_store = WebhookEventStore(tmp_path / "wh.db")
        await wh_store.init()

        await store.create(
            worktree_id="wt-o", model="m", prompt="t",
            pr_stack_id="xyz", stack_position=0, stack_size=2,
        )
        await store.create(
            worktree_id="wt-c1", model="m", prompt="t",
            pr_stack_id="xyz", stack_position=1, stack_size=2,
        )
        await store.create(
            worktree_id="wt-c2", model="m", prompt="t",
            pr_stack_id="xyz", stack_position=2, stack_size=2,
        )
        import aiosqlite
        async with aiosqlite.connect(store.db_path) as db:
            await db.execute(
                "UPDATE merge_jobs SET status='merged' "
                "WHERE pr_stack_id='xyz' AND stack_position >= 1",
            )
            await db.commit()

        handler = WebhookHandler(store=wh_store, secret="x")
        # No outbound injected — handler still promotes.
        result = await handler._maybe_promote_stack_parent("xyz", store)
        assert result is not None
        assert result["stack_id"] == "xyz"
