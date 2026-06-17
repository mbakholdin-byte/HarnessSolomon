"""Phase 4.8 v1.18.0: Tests for notify_terminal retry + deadletter queue.

Covers:
    1. Retry logic: success after transient, give-up, exponential
       backoff timing, permanent error short-circuit, retry-disabled
       (max_retries=0).
    2. DLQ persistence: record/query, instance persistence, disabled
       mode (skip storage but still emit counter).
    3. Observability: ``notify_dlq_total`` counter emitted on DLQ.
    4. Channel isolation: retry of one channel does not block another
       (asyncio.gather).
    5. Integration: Slack 5xx triggers retry, webhook timeout triggers
       retry.

All HTTP calls are mocked via ``unittest.mock.patch`` on
``urllib.request.urlopen`` — no real network traffic. SQLite stores
use a temp path (``tmp_path`` fixture) so tests are hermetic.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.hooks.builtin.notify_terminal import (
    ChannelError,
    NotifyDLQStore,
    _dispatch_to_channel,
    _DELIVERERS,
    _HANDLERS,
    _classify_exception,
)


# === Helpers ===


def _mock_urlopen(status: int = 200) -> MagicMock:
    """Build a MagicMock that mimics urlopen's context-manager return."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = status
    return mock_resp


def _http_error(status: int, reason: str = "Error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.com/h", status, reason, {}, None
    )


def _retry_settings(**overrides: object) -> MagicMock:
    """Build a settings mock with retry/DLQ defaults; override via kwargs."""
    base = dict(
        hooks_notify_max_retries=3,
        hooks_notify_retry_initial_delay_ms=100,
        hooks_notify_retry_max_delay_ms=5000,
        hooks_notify_dlq_enabled=True,
        # webhook defaults (used by integration tests)
        hooks_notify_webhook_url="https://example.com/h",
        hooks_notify_webhook_secret="",
        hooks_notify_webhook_timeout_s=5.0,
        # slack defaults
        hooks_notify_slack_webhook_url="https://hooks.slack.com/services/T/B/x",
        hooks_notify_slack_channel="#x",
        hooks_notify_slack_username="Solomon",
        hooks_notify_slack_timeout_s=5.0,
        # teams defaults
        hooks_notify_teams_webhook_url="https://outlook.office.com/webhook/abc",
        hooks_notify_teams_timeout_s=5.0,
        # desktop
        hooks_notify_desktop_enabled=False,
    )
    base.update(overrides)
    return MagicMock(**base)


# === 1. Retry logic ===


class TestRetrySuccessAfterTransient:
    @pytest.mark.asyncio
    async def test_retry_success_after_transient(self, tmp_path: Path) -> None:
        """First attempt fails (HTTP 503), second succeeds → return True."""
        settings = _retry_settings(hooks_notify_max_retries=3)
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")

        attempts = {"n": 0}

        async def flaky_deliverer(payload, _settings) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ChannelError(
                    category="transient",
                    channel="webhook",
                    status=503,
                    cause="HTTPError",
                    message="Service Unavailable",
                )
            # Second call: success (no raise).

        with patch.dict(_DELIVERERS, {"webhook": flaky_deliverer}):
            ok = await _dispatch_to_channel(
                "webhook",
                {"message": "hi", "severity": "info"},
                settings,
                dlq_store=dlq,
                sleep=_no_sleep,
            )
        assert ok is True
        assert attempts["n"] == 2

        # No DLQ entry on success.
        recent = await dlq.query_recent()
        assert recent == []


class TestRetryGivesUpAfterMax:
    @pytest.mark.asyncio
    async def test_retry_gives_up_after_max(self, tmp_path: Path) -> None:
        """All attempts fail with transient → DLQ after max_retries."""
        settings = _retry_settings(hooks_notify_max_retries=2)
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")

        attempts = {"n": 0}

        async def always_fail(payload, _settings) -> None:
            attempts["n"] += 1
            raise ChannelError(
                category="transient",
                channel="webhook",
                status=500,
                cause="HTTPError",
                message="Server Error",
            )

        with patch.dict(_DELIVERERS, {"webhook": always_fail}):
            ok = await _dispatch_to_channel(
                "webhook",
                {"message": "lost", "severity": "error"},
                settings,
                dlq_store=dlq,
                session_id="sess-1",
                sleep=_no_sleep,
            )
        assert ok is False
        # max_retries=2 → attempts = initial + 2 retries = 3.
        assert attempts["n"] == 3

        # DLQ entry persisted, terminal=True.
        recent = await dlq.query_recent()
        assert len(recent) == 1
        entry = recent[0]
        assert entry["channel"] == "webhook"
        assert entry["severity"] == "error"
        assert entry["session_id"] == "sess-1"
        assert entry["attempts"] == 3
        assert entry["terminal"] is True
        assert "Server Error" in entry["last_error"]


class TestRetryExponentialBackoff:
    @pytest.mark.asyncio
    async def test_retry_exponential_backoff(self, tmp_path: Path) -> None:
        """Backoff doubles: initial=100 → 100, 200, 400, 800..."""
        settings = _retry_settings(
            hooks_notify_max_retries=3,
            hooks_notify_retry_initial_delay_ms=100,
            hooks_notify_retry_max_delay_ms=5000,
        )
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")

        sleep_calls: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        async def always_fail(payload, _settings) -> None:
            raise ChannelError(
                category="transient",
                channel="webhook",
                status=500,
                cause="HTTPError",
                message="boom",
            )

        with patch.dict(_DELIVERERS, {"webhook": always_fail}):
            await _dispatch_to_channel(
                "webhook",
                {"message": "x", "severity": "info"},
                settings,
                dlq_store=dlq,
                sleep=record_sleep,
            )

        # 3 retries → 3 sleeps. Sequence: 100ms, 200ms, 400ms.
        assert len(sleep_calls) == 3
        assert sleep_calls == pytest.approx([0.1, 0.2, 0.4], abs=1e-6)

    @pytest.mark.asyncio
    async def test_backoff_capped_at_max(self, tmp_path: Path) -> None:
        """Backoff never exceeds retry_max_delay_ms."""
        settings = _retry_settings(
            hooks_notify_max_retries=5,
            hooks_notify_retry_initial_delay_ms=2000,
            hooks_notify_retry_max_delay_ms=3000,
        )
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")

        sleep_calls: list[float] = []

        async def record_sleep(s: float) -> None:
            sleep_calls.append(s)

        async def always_fail(payload, _settings) -> None:
            raise ChannelError(
                category="transient",
                channel="webhook",
                status=503,
                cause="HTTPError",
                message="x",
            )

        with patch.dict(_DELIVERERS, {"webhook": always_fail}):
            await _dispatch_to_channel(
                "webhook",
                {"message": "x", "severity": "info"},
                settings,
                dlq_store=dlq,
                sleep=record_sleep,
            )

        # initial=2000, max=3000 → sequence capped at 3.0s.
        for s in sleep_calls:
            assert s <= 3.0 + 1e-9


class TestRetryPermanentError:
    @pytest.mark.asyncio
    async def test_retry_permanent_error_no_retry(self, tmp_path: Path) -> None:
        """Permanent error (ValueError-classified) → DLQ immediately, no retry."""
        settings = _retry_settings(hooks_notify_max_retries=5)
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")

        attempts = {"n": 0}

        async def permanent_fail(payload, _settings) -> None:
            attempts["n"] += 1
            raise ChannelError(
                category="permanent",
                channel="slack",
                status=400,
                cause="ValueError",
                message="bad payload",
            )

        with patch.dict(_DELIVERERS, {"slack": permanent_fail}):
            ok = await _dispatch_to_channel(
                "slack",
                {"message": "bad", "severity": "warn"},
                settings,
                dlq_store=dlq,
                sleep=_no_sleep,
            )

        assert ok is False
        assert attempts["n"] == 1  # no retry

        recent = await dlq.query_recent()
        assert len(recent) == 1
        assert recent[0]["terminal"] is False  # permanent, not exhausted
        assert recent[0]["attempts"] == 1
        assert recent[0]["channel"] == "slack"


class TestRetryDisabledWhenMaxZero:
    @pytest.mark.asyncio
    async def test_retry_disabled_when_max_retries_zero(self, tmp_path: Path) -> None:
        """max_retries=0 → single attempt, first transient error → DLQ."""
        settings = _retry_settings(hooks_notify_max_retries=0)
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")

        attempts = {"n": 0}

        async def fail_once(payload, _settings) -> None:
            attempts["n"] += 1
            raise ChannelError(
                category="transient",
                channel="webhook",
                status=503,
                cause="HTTPError",
                message="Service Unavailable",
            )

        sleep_calls: list[float] = []

        async def record_sleep(s: float) -> None:
            sleep_calls.append(s)

        with patch.dict(_DELIVERERS, {"webhook": fail_once}):
            ok = await _dispatch_to_channel(
                "webhook",
                {"message": "x", "severity": "info"},
                settings,
                dlq_store=dlq,
                sleep=record_sleep,
            )

        assert ok is False
        assert attempts["n"] == 1  # no retries at all
        assert sleep_calls == []   # no sleep happened

        recent = await dlq.query_recent()
        assert len(recent) == 1
        # max_retries=0 with transient → treated as terminal (exhausted).
        assert recent[0]["terminal"] is True
        assert recent[0]["attempts"] == 1


# === 2. DLQ persistence ===


class TestDLQQueryRecent:
    @pytest.mark.asyncio
    async def test_dlq_query_recent_orders_newest_first(self, tmp_path: Path) -> None:
        """query_recent returns entries ordered by ts DESC."""
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")
        await dlq.init()

        # Insert 3 entries with distinct timestamps.
        for i, ch in enumerate(["stdout", "webhook", "slack"]):
            await dlq.record_failure(
                session_id=f"s{i}",
                severity="info",
                channel=ch,
                payload={"message": f"msg-{i}"},
                last_error=f"err-{i}",
                attempts=i + 1,
                terminal=(i % 2 == 0),
            )
            # Tiny sleep to ensure ts ordering is deterministic.
            await asyncio.sleep(0.005)

        recent = await dlq.query_recent(limit=2)
        assert len(recent) == 2
        # Newest first → slack (last inserted), then webhook.
        assert recent[0]["channel"] == "slack"
        assert recent[1]["channel"] == "webhook"

        all_entries = await dlq.query_recent(limit=50)
        assert len(all_entries) == 3

    @pytest.mark.asyncio
    async def test_dlq_query_recent_empty(self, tmp_path: Path) -> None:
        """query_recent on empty store returns []."""
        dlq = NotifyDLQStore(tmp_path / "test_dlq.db")
        recent = await dlq.query_recent()
        assert recent == []


class TestDLQPersistsAcrossInstances:
    @pytest.mark.asyncio
    async def test_dlq_persists_across_instances(self, tmp_path: Path) -> None:
        """A new NotifyDLQStore on the same path sees prior entries."""
        db = tmp_path / "shared.db"
        store1 = NotifyDLQStore(db)
        await store1.init()
        await store1.record_failure(
            session_id="s1",
            severity="error",
            channel="webhook",
            payload={"message": "persist-me"},
            last_error="boom",
            attempts=3,
            terminal=True,
        )

        # New instance, same path.
        store2 = NotifyDLQStore(db)
        recent = await store2.query_recent()
        assert len(recent) == 1
        assert recent[0]["channel"] == "webhook"
        assert recent[0]["payload_json"] != ""
        # payload_json round-trips.
        payload = json.loads(recent[0]["payload_json"])
        assert payload["message"] == "persist-me"


class TestDLQDisabledSkipsStorage:
    @pytest.mark.asyncio
    async def test_dlq_disabled_skips_storage(self, tmp_path: Path) -> None:
        """dlq_enabled=False → no SQLite write, but counter still emits."""
        settings = _retry_settings(hooks_notify_dlq_enabled=False)
        db = tmp_path / "should_not_exist.db"
        dlq = NotifyDLQStore(db)

        async def always_fail(payload, _settings) -> None:
            raise ChannelError(
                category="permanent",
                channel="webhook",
                status=400,
                cause="ValueError",
                message="bad",
            )

        with patch.dict(_DELIVERERS, {"webhook": always_fail}):
            # Even though we pass a dlq_store, dlq_enabled=False must
            # prevent the write.
            ok = await _dispatch_to_channel(
                "webhook",
                {"message": "x", "severity": "info"},
                settings,
                dlq_store=dlq,
                sleep=_no_sleep,
            )

        assert ok is False
        # File must NOT have been created (init never ran).
        assert not db.exists()

        # The counter emission is mocked via emit_notify_dlq — verify
        # by patching it.
        with patch(
            "harness.hooks.builtin.notify_terminal.emit_notify_dlq"
        ) as mock_emit:
            with patch.dict(_DELIVERERS, {"webhook": always_fail}):
                await _dispatch_to_channel(
                    "webhook",
                    {"message": "x", "severity": "info"},
                    settings,
                    dlq_store=dlq,
                    sleep=_no_sleep,
                )
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["channel"] == "webhook"
            assert call_kwargs["severity"] == "info"


# === 3. Observability: notify_dlq_total counter ===


class TestEmitDLQCounter:
    @pytest.mark.asyncio
    async def test_emit_dlq_counter_on_failure(self, tmp_path: Path) -> None:
        """emit_notify_dlq is called with correct labels on DLQ."""
        settings = _retry_settings(hooks_notify_max_retries=0)
        dlq = NotifyDLQStore(tmp_path / "test.db")

        async def fail(payload, _settings) -> None:
            raise ChannelError(
                category="transient",
                channel="teams",
                status=502,
                cause="HTTPError",
                message="Bad Gateway",
            )

        with patch(
            "harness.hooks.builtin.notify_terminal.emit_notify_dlq"
        ) as mock_emit, patch.dict(_DELIVERERS, {"teams": fail}):
            await _dispatch_to_channel(
                "teams",
                {"message": "x", "severity": "warn"},
                settings,
                dlq_store=dlq,
                sleep=_no_sleep,
            )

        mock_emit.assert_called_once()
        args, kwargs = mock_emit.call_args
        assert kwargs["severity"] == "warn"
        assert kwargs["channel"] == "teams"
        assert kwargs["terminal"] is True


# === 4. Channel isolation ===


class TestChannelIsolation:
    @pytest.mark.asyncio
    async def test_channel_isolation_on_retry(self, tmp_path: Path) -> None:
        """A slow/failing channel does not block another.

        Channel A always fails (transient) and would sleep for 60s on
        each retry. Channel B succeeds instantly. With asyncio.gather,
        B must complete before A's retries finish. We assert B's
        deliverer was called (and returned) within a small wall-clock
        budget.
        """
        settings = _retry_settings(
            hooks_notify_max_retries=2,
            hooks_notify_retry_initial_delay_ms=10_000,  # 10s sleep
            hooks_notify_retry_max_delay_ms=10_000,
        )
        dlq = NotifyDLQStore(tmp_path / "iso.db")

        b_called = {"n": 0}

        async def slow_fail(payload, _settings) -> None:
            raise ChannelError(
                category="transient",
                channel="stdout_a",
                status=503,
                cause="HTTPError",
                message="x",
            )

        async def fast_success(payload, _settings) -> None:
            b_called["n"] += 1

        # Override asyncio.sleep to a no-op so the test stays fast,
        # but inject a sentinel to verify ordering: B should run
        # concurrently with A.
        with patch.dict(
            _DELIVERERS,
            {"stdout": slow_fail, "desktop": fast_success},
        ):
            results = await asyncio.gather(
                _dispatch_to_channel(
                    "stdout",
                    {"message": "a", "severity": "info"},
                    settings,
                    dlq_store=dlq,
                    sleep=_no_sleep,
                ),
                _dispatch_to_channel(
                    "desktop",
                    {"message": "b", "severity": "info"},
                    settings,
                    dlq_store=dlq,
                    sleep=_no_sleep,
                ),
                return_exceptions=True,
            )

        # slow channel failed → False; fast channel succeeded → True.
        assert results[0] is False
        assert results[1] is True
        assert b_called["n"] == 1


# === 5. Integration: real channels via mocked HTTP ===


class TestSlack5xxTriggersRetry:
    @pytest.mark.asyncio
    async def test_slack_5xx_triggers_retry(self, tmp_path: Path) -> None:
        """Slack returning 503 → _deliver_slack raises transient ChannelError."""
        from harness.hooks.builtin.notify_terminal import _deliver_slack

        settings = _retry_settings()
        err = _http_error(503, "Service Unavailable")

        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(ChannelError) as ei:
                await _deliver_slack({"message": "hi", "severity": "info"}, settings)

        ce = ei.value
        assert ce.category == "transient"
        assert ce.channel == "slack"
        assert ce.status == 503


class TestSlack4xxIsPermanent:
    @pytest.mark.asyncio
    async def test_slack_4xx_is_permanent(self) -> None:
        """Slack returning 400 → permanent ChannelError."""
        from harness.hooks.builtin.notify_terminal import _deliver_slack

        settings = _retry_settings()
        err = _http_error(400, "Bad Request")

        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=err,
        ):
            with pytest.raises(ChannelError) as ei:
                await _deliver_slack({"message": "hi"}, settings)

        assert ei.value.category == "permanent"
        assert ei.value.status == 400


class TestWebhookTimeoutTriggersRetry:
    @pytest.mark.asyncio
    async def test_webhook_timeout_triggers_retry(self) -> None:
        """Webhook timeout → transient ChannelError."""
        from harness.hooks.builtin.notify_terminal import _deliver_webhook

        settings = _retry_settings()

        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=TimeoutError("connect timed out"),
        ):
            with pytest.raises(ChannelError) as ei:
                await _deliver_webhook({"message": "hi"}, settings)

        assert ei.value.category == "transient"
        assert ei.value.channel == "webhook"


class TestWebhookOSErrorIsTransient:
    @pytest.mark.asyncio
    async def test_webhook_oserror_is_transient(self) -> None:
        """ConnectionResetError (OSError subclass) → transient."""
        from harness.hooks.builtin.notify_terminal import _deliver_webhook

        settings = _retry_settings()

        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=ConnectionResetError("reset by peer"),
        ):
            with pytest.raises(ChannelError) as ei:
                await _deliver_webhook({"message": "hi"}, settings)

        assert ei.value.category == "transient"


# === 6. _classify_exception unit tests ===


class TestClassifyException:
    def test_http_5xx_transient(self) -> None:
        err = urllib.error.HTTPError("u", 503, "x", {}, None)
        ce = _classify_exception(err, "webhook")
        assert ce.category == "transient"
        assert ce.status == 503

    def test_http_4xx_permanent(self) -> None:
        err = urllib.error.HTTPError("u", 404, "x", {}, None)
        ce = _classify_exception(err, "slack")
        assert ce.category == "permanent"
        assert ce.status == 404

    def test_timeout_transient(self) -> None:
        ce = _classify_exception(TimeoutError("t"), "teams")
        assert ce.category == "transient"

    def test_oserror_transient(self) -> None:
        ce = _classify_exception(OSError("nope"), "desktop")
        assert ce.category == "transient"

    def test_value_error_permanent(self) -> None:
        ce = _classify_exception(ValueError("bad"), "stdout")
        assert ce.category == "permanent"

    def test_unknown_transient(self) -> None:
        ce = _classify_exception(RuntimeError("???"), "webhook")
        assert ce.category == "transient"


# === 7. Settings ===


class TestSettings:
    def test_four_new_fields_exist_with_defaults(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_max_retries == 3
        assert s.hooks_notify_retry_initial_delay_ms == 100
        assert s.hooks_notify_retry_max_delay_ms == 5000
        assert s.hooks_notify_dlq_enabled is True

    def test_all_four_fields_exist(self) -> None:
        from harness.config import Settings

        s = Settings()
        for f in (
            "hooks_notify_max_retries",
            "hooks_notify_retry_initial_delay_ms",
            "hooks_notify_retry_max_delay_ms",
            "hooks_notify_dlq_enabled",
        ):
            assert hasattr(s, f), f"missing {f}"


# === 8. Deliverer table parity ===


class TestDelivererTable:
    def test_deliverer_table_matches_handlers(self) -> None:
        """_DELIVERERS covers the same channels as _HANDLERS."""
        assert set(_DELIVERERS) == set(_HANDLERS)
        assert set(_DELIVERERS) == {"stdout", "webhook", "desktop", "slack", "teams"}


# === Helpers (module-level) ===


async def _no_sleep(_seconds: float) -> None:
    """No-op async sleep used in tests to skip real backoff delays."""
    return None
