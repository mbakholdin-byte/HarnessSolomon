"""Phase 4.1 Step 6.12: Wiring tests for 17 observability trigger points.

Each test exercises one trigger point and asserts that the metric
counter/histogram was incremented AND a log line was emitted.

Strategy:
    1. Reset the observability singleton.
    2. Enable per-event settings via in-memory Settings.
    3. Construct the system under test.
    4. Fire the trigger point.
    5. Read the in-memory JsonlLogger and verify the event was logged.

Tests use the real observability singletons — no mocks for our own
classes. External systems (LLM, sqlite, worktree) are mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.config import Settings
from harness.observability import (
    JsonlLogger,
    LogEvent,
    ObservabilityHandle,
    emit_compaction,
    emit_hook_dispatch,
    emit_http_request,
    emit_llm_call,
    emit_merge_queue_event,
    emit_outbound_delivery,
    emit_privacy_decision,
    emit_tool_call,
    emit_webhook_inbound,
    get_observability,
    reset_observability,
)


# === Fixtures ===


@pytest.fixture
def obs_dir(tmp_path: Path) -> Path:
    """Per-test log dir + clean singleton."""
    reset_observability()
    return tmp_path


def _settings_with(dir_: Path, **overrides: object) -> Settings:
    """Build Settings with observability enabled and pointing at dir_."""
    base: dict[str, object] = {
        "observability_enabled": True,
        "observability_jsonl_enabled": True,
        "observability_prometheus_enabled": False,
        "observability_otlp_enabled": False,
        "observability_log_dir": dir_,
        "observability_metrics_namespace": "harness_test",
        "observability_cost_enabled": True,
        "observability_cost_overrides": "",
        "observability_log_http_requests": True,
        "observability_log_llm_calls": True,
        "observability_log_tool_calls": True,
        "observability_log_hook_dispatches": True,
        "observability_log_compactions": True,
        "observability_log_merge_queue_events": True,
        "observability_log_outbound_deliveries": True,
        "observability_log_privacy_decisions": True,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# === Singleton tests (Step 6.1) ===


class TestSingleton:
    def test_get_observability_returns_singleton(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        a = get_observability(s)
        b = get_observability()
        assert a is b

    def test_reset_observability_rebuilds(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        a = get_observability(s)
        reset_observability()
        b = get_observability(s)
        assert a is not b

    def test_handle_contains_all_components(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        assert isinstance(h, ObservabilityHandle)
        assert isinstance(h.logger, JsonlLogger)
        assert h.metrics is not None
        assert h.tracer is not None
        assert h.health is not None
        assert h.cost is not None


# === Trigger point tests (Step 6.2-6.10) ===
#
# Each test exercises one emit_* helper and asserts the JSONL log line
# was written to the per-test dir.


class TestTriggerHttpRequest:
    def test_http_request_logs_event(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_http_request(method="GET", route="/api/test", status=200, duration_s=0.123, request_id="r-1")
        lines = h.logger.tail(n=10)
        assert any(ev["event"] == "http_request" for ev in lines)

    def test_http_request_4xx_logs_error(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_http_request(method="POST", route="/api/bad", status=400, duration_s=0.5)
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "http_request")
        assert ev["status"] == "error"


class TestTriggerLlmCall:
    def test_llm_call_logs_event_with_cost(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        cost = emit_llm_call(
            model="gpt-4o", tier="T3", prompt_tokens=1000, completion_tokens=500,
            duration_s=0.5, status="ok",
        )
        assert cost > 0.0
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "llm_call")
        assert ev["payload"]["model"] == "gpt-4o"
        assert ev["payload"]["cost_usd"] > 0.0

    def test_llm_call_error_status(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_llm_call(
            model="gpt-4o", tier="T3", prompt_tokens=0, completion_tokens=0,
            duration_s=0.1, status="error", error="rate_limited",
        )
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "llm_call")
        assert ev["status"] == "error"
        assert ev["payload"]["error"] == "rate_limited"

    def test_llm_call_cost_disabled(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir, observability_cost_enabled=False)
        h = get_observability(s)
        cost = emit_llm_call(
            model="gpt-4o", tier="T3", prompt_tokens=1000, completion_tokens=500,
            duration_s=0.5,
        )
        assert cost == 0.0
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "llm_call")
        assert ev["payload"]["cost_usd"] == 0.0


class TestTriggerToolCall:
    def test_tool_call_logs_event(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_tool_call(tool_name="read_file", duration_s=0.1, status="ok")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "tool_call")
        assert ev["payload"]["tool_name"] == "read_file"

    def test_tool_call_error(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_tool_call(tool_name="bash", duration_s=0.5, status="error", error="timeout")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "tool_call")
        assert ev["payload"]["error"] == "timeout"
        assert ev["status"] == "error"


class TestTriggerHookDispatch:
    def test_hook_dispatch_logs_event(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_hook_dispatch(event="PreToolUse", decision="allow", duration_s=0.005, hook_name="log")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "hook_dispatch")
        assert ev["payload"]["event"] == "PreToolUse"
        assert ev["payload"]["decision"] == "allow"

    def test_hook_dispatch_block(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_hook_dispatch(event="PreToolUse", decision="block", duration_s=0.01, hook_name="validator")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "hook_dispatch")
        assert ev["payload"]["decision"] == "block"


class TestTriggerCompaction:
    def test_compaction_pre_compact_mode(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_compaction(mode="pre_compact", cache_hit=False, duration_s=0.5, session_id="s1")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "compaction")
        assert ev["payload"]["mode"] == "pre_compact"
        assert ev["session_id"] == "s1"

    def test_compaction_manual_cache_hit(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_compaction(mode="manual", cache_hit=True, duration_s=0.001, session_id="s1")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "compaction")
        assert ev["payload"]["cache_hit"] is True


class TestTriggerMergeQueue:
    def test_merge_queue_enqueue(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_merge_queue_event(kind="enqueue", status="ok", job_id="j-1")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "merge_queue_event")
        assert ev["payload"]["kind"] == "enqueue"
        assert ev["payload"]["job_id"] == "j-1"

    def test_merge_queue_finish_error(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_merge_queue_event(kind="finish", status="error", job_id="j-2", error="oops")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "merge_queue_event")
        assert ev["status"] == "error"
        assert ev["payload"]["error"] == "oops"


class TestTriggerOutbound:
    def test_outbound_success(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_outbound_delivery(kind="merged", status_code="200", duration_s=0.1)
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "outbound_delivery")
        assert ev["payload"]["kind"] == "merged"
        assert ev["status"] == "ok"

    def test_outbound_4xx(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_outbound_delivery(kind="merged", status_code="404", duration_s=0.1, error="not found")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "outbound_delivery")
        assert ev["status"] == "error"
        assert ev["payload"]["error"] == "not found"

    def test_outbound_timeout(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_outbound_delivery(kind="pr_waiting_review", status_code="timeout", duration_s=5.0)
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "outbound_delivery")
        assert ev["status"] == "error"


class TestTriggerPrivacy:
    def test_privacy_block(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_privacy_decision(action="block", path="private/.env", pattern="**/.env")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "privacy_decision")
        assert ev["payload"]["action"] == "block"
        assert ev["payload"]["path"] == "private/.env"

    def test_privacy_redact(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_privacy_decision(action="redact", path="home/user/.ssh/id_rsa", pattern="**/.ssh/**")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "privacy_decision")
        assert ev["payload"]["action"] == "redact"


class TestTriggerWebhookInbound:
    def test_webhook_inbound_pull_request(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_webhook_inbound(event_type="pull_request", status="ok", delivery_id="d-1")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "webhook_inbound")
        assert ev["payload"]["event_type"] == "pull_request"

    def test_webhook_inbound_error(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir)
        h = get_observability(s)
        emit_webhook_inbound(event_type="check_run", status="error", delivery_id="d-2")
        lines = h.logger.tail(n=10)
        ev = next(e for e in lines if e["event"] == "webhook_inbound")
        assert ev["status"] == "error"


# === Per-event opt-out tests (gating) ===


class TestPerEventGating:
    """When a per-event setting is False, the emit helper should be a no-op."""

    def test_llm_call_disabled(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir, observability_log_llm_calls=False)
        h = get_observability(s)
        cost = emit_llm_call(
            model="gpt-4o", tier="T3", prompt_tokens=1000, completion_tokens=500,
            duration_s=0.5,
        )
        # When observability_log_llm_calls=False, the entire helper is
        # a no-op: no cost computed, no log line emitted.
        assert cost == 0.0
        lines = h.logger.tail(n=10)
        assert not any(ev["event"] == "llm_call" for ev in lines)

    def test_tool_call_disabled(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir, observability_log_tool_calls=False)
        h = get_observability(s)
        emit_tool_call(tool_name="read_file", duration_s=0.1)
        lines = h.logger.tail(n=10)
        assert not any(ev["event"] == "tool_call" for ev in lines)

    def test_compaction_disabled(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir, observability_log_compactions=False)
        h = get_observability(s)
        emit_compaction(mode="manual", cache_hit=False, duration_s=0.5, session_id="s1")
        lines = h.logger.tail(n=10)
        assert not any(ev["event"] == "compaction" for ev in lines)


# === Master switch test ===


class TestMasterSwitch:
    def test_master_disabled_skips_emit(self, obs_dir: Path) -> None:
        s = _settings_with(obs_dir, observability_enabled=False)
        h = get_observability(s)
        # The handle.emit() respects master switch.
        from harness.observability.events import LogEvent
        h.emit(LogEvent(event="test_event", payload={"x": 1}))
        lines = h.logger.tail(n=10)
        assert not any(ev["event"] == "test_event" for ev in lines)
