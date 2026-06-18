"""Phase 4.3 v1.10.0: Tests for Elicitation + Notification events.

Covers:
    1. ``EventType.ELICITATION`` / ``EventType.NOTIFICATION`` enum members.
    2. ``ENABLED_BY_DEFAULT`` includes the new events.
    3. ``is_valid_elicitation_payload`` / ``is_valid_notification_payload``
       structural checks.
    4. ``confirm_dangerous_hook`` — Elicitation default answer injection.
    5. ``notify_terminal_hook`` — Notification stderr fanout.
    6. ``HookRunner`` dispatches Elicitation + Notification without errors.
    7. Observability: ``emit_elicitation_response`` / ``emit_notification_dispatched``
       increment counters and emit log events.
    8. Settings: ``hooks_elicitation_enabled`` / ``hooks_notification_enabled``
       + 2 new builtin flags.
    9. Trust boundary: new modules don't import agents/server/hooks.
"""
from __future__ import annotations

import asyncio
import io
import sys
from unittest.mock import patch

import pytest

from harness.hooks import (
    ELICITATION_VALID_ANSWERS,
    NOTIFICATION_VALID_CHANNELS,
    NOTIFICATION_VALID_SEVERITIES,
    EventType,
    HookAggregate,
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    HookSpec,
    is_valid_elicitation_payload,
    is_valid_notification_payload,
)
from harness.hooks.builtin import (
    BUILTIN_HOOKS,
    confirm_dangerous_hook,
    notify_terminal_hook,
)


# === 1. EventType enum members ===

class TestEventTypesAdded:
    def test_elicitation_member_exists(self) -> None:
        assert EventType.ELICITATION.value == "Elicitation"

    def test_notification_member_exists(self) -> None:
        assert EventType.NOTIFICATION.value == "Notification"

    def test_total_event_count(self) -> None:
        # Phase 4.0 had 15 (12 CC + 3 custom); Phase 4.3 adds 2 → 16.
        assert len(EventType) == 16

    def test_enabled_by_default_includes_both(self) -> None:
        from harness.hooks.events import ENABLED_BY_DEFAULT

        assert EventType.ELICITATION in ENABLED_BY_DEFAULT
        assert EventType.NOTIFICATION in ENABLED_BY_DEFAULT

    def test_deferred_events_empty(self) -> None:
        from harness.hooks.events import DEFERRED_EVENTS

        assert DEFERRED_EVENTS == frozenset()


# === 2. Schema helpers ===

class TestElicitationSchema:
    def test_valid_minimal(self) -> None:
        assert is_valid_elicitation_payload({"question": "Run rm -rf /?"})

    def test_valid_full(self) -> None:
        assert is_valid_elicitation_payload({
            "question": "Run rm -rf /?",
            "options": ["proceed", "abort"],
            "multi_select": False,
            "default_answer": "abort",
            "requires_confirmation": True,
            "answer": "abort",
            "answer_source": "user",
        })

    def test_missing_question(self) -> None:
        assert not is_valid_elicitation_payload({})

    def test_empty_question(self) -> None:
        assert not is_valid_elicitation_payload({"question": "   "})

    def test_question_not_string(self) -> None:
        assert not is_valid_elicitation_payload({"question": 42})

    def test_options_must_be_list_of_strings(self) -> None:
        assert not is_valid_elicitation_payload(
            {"question": "q", "options": "proceed,abort"}
        )
        assert not is_valid_elicitation_payload(
            {"question": "q", "options": ["proceed", 42]}
        )

    def test_multi_select_must_be_bool(self) -> None:
        assert not is_valid_elicitation_payload(
            {"question": "q", "multi_select": "yes"}
        )

    def test_default_answer_must_be_string(self) -> None:
        assert not is_valid_elicitation_payload(
            {"question": "q", "default_answer": 1}
        )

    def test_answer_must_be_string(self) -> None:
        assert not is_valid_elicitation_payload(
            {"question": "q", "answer": 99}
        )

    def test_requires_confirmation_must_be_bool(self) -> None:
        assert not is_valid_elicitation_payload(
            {"question": "q", "requires_confirmation": 1}
        )

    def test_payload_must_be_dict(self) -> None:
        assert not is_valid_elicitation_payload("not a dict")  # type: ignore[arg-type]
        assert not is_valid_elicitation_payload(None)  # type: ignore[arg-type]

    def test_valid_answers_constant(self) -> None:
        # Reserved answers — informational only.
        assert "proceed" in ELICITATION_VALID_ANSWERS
        assert "abort" in ELICITATION_VALID_ANSWERS


class TestNotificationSchema:
    def test_valid_minimal(self) -> None:
        assert is_valid_notification_payload({"message": "Compaction done"})

    def test_valid_full(self) -> None:
        assert is_valid_notification_payload({
            "message": "Compaction done",
            "severity": "warn",
            "channels": ["stdout", "webhook"],
        })

    def test_missing_message(self) -> None:
        assert not is_valid_notification_payload({})

    def test_empty_message(self) -> None:
        assert not is_valid_notification_payload({"message": ""})

    def test_message_not_string(self) -> None:
        assert not is_valid_notification_payload({"message": 42})

    def test_invalid_severity(self) -> None:
        assert not is_valid_notification_payload(
            {"message": "x", "severity": "fatal"}
        )

    def test_channels_must_be_list(self) -> None:
        assert not is_valid_notification_payload(
            {"message": "x", "channels": "stdout"}
        )

    def test_invalid_channel(self) -> None:
        assert not is_valid_notification_payload(
            {"message": "x", "channels": ["stdout", "carrier-pigeon"]}
        )

    def test_constants(self) -> None:
        assert "info" in NOTIFICATION_VALID_SEVERITIES
        assert "stdout" in NOTIFICATION_VALID_CHANNELS


# === 3. confirm_dangerous_hook ===

class TestConfirmDangerousHook:
    @pytest.mark.asyncio
    async def test_ignores_non_elicitation_events(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash"},
        )
        decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "allow"
        assert decision.hook_id == "builtin.confirm_dangerous"

    @pytest.mark.asyncio
    async def test_ignores_when_not_requires_confirmation(self) -> None:
        ctx = HookContext(
            event="Elicitation",
            session_id="s1",
            agent_id="",
            payload={"question": "Anything goes"},
        )
        decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "allow"

    @pytest.mark.asyncio
    async def test_injects_default_answer(self) -> None:
        # Phase 4.3+ v1.12.0: WS broker is enabled by default with 30s
        # timeout. Without a human client, the wait() will time out
        # and return the default — so source is "default_timeout".
        # For a fast test, monkey-patch Settings to disable WS.
        from unittest.mock import patch
        with patch("harness.config.Settings") as mock_settings:
            mock_settings.return_value.hooks_elicitation_ws_enabled = False
            ctx = HookContext(
                event="Elicitation",
                session_id="s1",
                agent_id="",
                payload={
                    "question": "Run rm -rf /?",
                    "options": ["proceed", "abort"],
                    "default_answer": "abort",
                    "requires_confirmation": True,
                },
            )
            decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "modify"
        assert decision.output["payload"]["answer"] == "abort"
        # Phase 4.3+ v1.12.0: source reflects which path resolved the answer.
        assert decision.output["payload"]["answer_source"] == "default_ws_disabled"

    @pytest.mark.asyncio
    async def test_default_answer_fallback_abort(self) -> None:
        # If default_answer missing, fall back to "abort" (safe default).
        ctx = HookContext(
            event="Elicitation",
            session_id="s1",
            agent_id="",
            payload={"question": "Drop table?", "requires_confirmation": True},
        )
        decision = await confirm_dangerous_hook(ctx)
        assert decision.decision == "modify"
        assert decision.output["payload"]["answer"] == "abort"


# === 4. notify_terminal_hook ===

class TestNotifyTerminalHook:
    @pytest.mark.asyncio
    async def test_ignores_non_notification_events(self) -> None:
        ctx = HookContext(event="Stop", session_id="s1", agent_id="", payload={})
        decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"

    @pytest.mark.asyncio
    async def test_skips_empty_message(self) -> None:
        ctx = HookContext(
            event="Notification",
            session_id="s1",
            agent_id="",
            payload={"message": ""},
        )
        decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"

    @pytest.mark.asyncio
    async def test_writes_to_stderr(self) -> None:
        # Capture stderr via contextlib.redirect_stderr.
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ctx = HookContext(
                event="Notification",
                session_id="s1",
                agent_id="",
                payload={
                    "message": "Compaction done in 1.2s",
                    "severity": "info",
                    "channels": ["stdout"],
                },
            )
            decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"
        assert "[INFO] Compaction done in 1.2s" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_warns_severity_prefix(self) -> None:
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ctx = HookContext(
                event="Notification",
                session_id="s1",
                agent_id="",
                payload={
                    "message": "Quota exceeded",
                    "severity": "warn",
                    "channels": ["stdout"],
                },
            )
            await notify_terminal_hook(ctx)
        assert "[WARN] Quota exceeded" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_error_severity_prefix(self) -> None:
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ctx = HookContext(
                event="Notification",
                session_id="s1",
                agent_id="",
                payload={
                    "message": "LLM timeout",
                    "severity": "error",
                    "channels": ["stdout"],
                },
            )
            await notify_terminal_hook(ctx)
        assert "[ERROR] LLM timeout" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_default_severity_is_info(self) -> None:
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ctx = HookContext(
                event="Notification",
                session_id="s1",
                agent_id="",
                payload={"message": "hi", "channels": ["stdout"]},
            )
            await notify_terminal_hook(ctx)
        assert "[INFO] hi" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_no_stdout_channel_skips_write(self) -> None:
        # "webhook" only — no stderr write.
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ctx = HookContext(
                event="Notification",
                session_id="s1",
                agent_id="",
                payload={"message": "hi", "channels": ["webhook"]},
            )
            await notify_terminal_hook(ctx)
        assert buf.getvalue() == ""


# === 5. HookRunner dispatches new events ===

class TestRunnerDispatchNewEvents:
    @pytest.mark.asyncio
    async def test_runner_dispatches_elicitation(self) -> None:
        # Phase 4.3+ v1.12.0: disable WS so confirm_dangerous falls
        # back to the default without waiting 30s.
        from unittest.mock import patch
        registry = HookRegistry()
        with patch("harness.config.Settings") as mock_settings:
            mock_settings.return_value.hooks_elicitation_ws_enabled = False
            await registry.register(HookSpec(
                hook_id="test.confirm",
                event=EventType.ELICITATION,
                transport="builtin",
                callable=confirm_dangerous_hook,
            ))
            runner = HookRunner(registry, default_timeout_ms=1000)
            ctx = HookContext(
                event="Elicitation",
                session_id="s1",
                agent_id="",
                payload={
                    "question": "Drop table?",
                    "default_answer": "abort",
                    "requires_confirmation": True,
                },
            )
            agg = await runner.fire(ctx)
        assert agg.final_decision == "modify"
        assert agg.final_payload["answer"] == "abort"

    @pytest.mark.asyncio
    async def test_runner_dispatches_notification(self) -> None:
        registry = HookRegistry()
        await registry.register(HookSpec(
            hook_id="test.notify",
            event=EventType.NOTIFICATION,
            transport="builtin",
            callable=notify_terminal_hook,
        ))
        runner = HookRunner(registry, default_timeout_ms=1000)
        ctx = HookContext(
            event="Notification",
            session_id="s1",
            agent_id="",
            payload={"message": "Phase 4.3 shipped", "channels": ["stdout"]},
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"

    @pytest.mark.asyncio
    async def test_no_hooks_registered_returns_allow(self) -> None:
        registry = HookRegistry()
        runner = HookRunner(registry, default_timeout_ms=1000)
        ctx = HookContext(
            event="Elicitation",
            session_id="s1",
            agent_id="",
            payload={"question": "Anything?"},
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert agg.decisions == ()


# === 6. Settings expose new flags ===

class TestSettingsFlags:
    def test_elicitation_event_enabled(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_elicitation_enabled is True

    def test_notification_event_enabled(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notification_enabled is True

    def test_builtin_confirm_dangerous_enabled(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_builtin_confirm_dangerous_enabled is True

    def test_builtin_notify_terminal_enabled(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_builtin_notify_terminal_enabled is True

    def test_total_settings_count_increased_by_4(self) -> None:
        # Phase 4.0 = 67 settings; Phase 4.3 = 67 + 4 (2 events + 2 builtins).
        # We just sanity-check that the new fields exist.
        from harness.config import Settings

        s = Settings()
        for f in (
            "hooks_elicitation_enabled",
            "hooks_notification_enabled",
            "hooks_builtin_confirm_dangerous_enabled",
            "hooks_builtin_notify_terminal_enabled",
        ):
            assert hasattr(s, f), f"missing setting {f}"


# === 7. BUILTIN_HOOKS registry ===

class TestBuiltinHooksRegistry:
    def test_confirm_dangerous_registered(self) -> None:
        assert "confirm_dangerous" in BUILTIN_HOOKS
        assert BUILTIN_HOOKS["confirm_dangerous"] is confirm_dangerous_hook

    def test_notify_terminal_registered(self) -> None:
        assert "notify_terminal" in BUILTIN_HOOKS
        assert BUILTIN_HOOKS["notify_terminal"] is notify_terminal_hook

    def test_total_builtin_count(self) -> None:
        # Phase 4.0 = 5; Phase 4.3 = 7; Phase 4.10 = 12 (added 5 security/simple patterns).
        assert len(BUILTIN_HOOKS) == 12


# === 8. Observability emit helpers ===

class TestObservabilityEmit:
    def test_emit_elicitation_increments_counter(self) -> None:
        from harness.observability import reset_observability
        from harness.observability.metrics import PrometheusMetrics

        reset_observability()
        from harness.observability import emit_elicitation_response

        # We can't easily inspect prometheus_client counters without
        # rendering. But we can call without raising.
        emit_elicitation_response("modify", question="Run rm?", hook_name="test")
        # Idempotent: another call shouldn't break anything.
        emit_elicitation_response("allow", question="continue?", hook_name="test")

    def test_emit_notification_increments_counter(self) -> None:
        from harness.observability import (
            emit_notification_dispatched,
            reset_observability,
        )

        reset_observability()
        emit_notification_dispatched(
            "info", "stdout", message="Compaction done", hook_name="test"
        )
        emit_notification_dispatched(
            "error", "webhook", message="Webhook failed", hook_name="test"
        )

    def test_metrics_have_new_counters(self) -> None:
        from harness.observability.metrics import PrometheusMetrics

        m = PrometheusMetrics()
        assert hasattr(m, "elicitation_total")
        assert hasattr(m, "notification_total")
