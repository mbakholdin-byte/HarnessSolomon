"""Phase 4.6 v1.16.0: Tests for Slack + Teams Notification channels.

Covers:
    1. Slack channel: POST to webhook, severity→color, disabled when URL empty,
       payload structure, webhook URL redaction in logs.
    2. Teams channel: POST MessageCard, severity→themeColor, disabled when URL
       empty.
    3. Dispatcher: both channels registered in ``_HANDLERS``.
    4. Settings: 6 new fields exist with correct defaults.

Mirrors the structure of ``test_notify_terminal_channels.py``. All HTTP
calls are mocked via ``unittest.mock.patch`` on
``urllib.request.urlopen`` — no real network traffic.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from harness.hooks import HookContext
from harness.hooks.builtin.notify_terminal import (
    _HANDLERS,
    _handle_slack,
    _handle_teams,
    _redact_webhook_url,
    _severity_to_slack_color,
    _severity_to_teams_color,
)


# === Helpers ===

def _mock_urlopen_200() -> MagicMock:
    """Build a MagicMock that mimics urlopen's context-manager return."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    return mock_resp


def _slack_settings(**overrides: object) -> MagicMock:
    """Build a settings mock with Slack defaults; override via kwargs."""
    base = dict(
        hooks_notify_slack_webhook_url="https://hooks.slack.com/services/T0/B1/SECRET",
        hooks_notify_slack_channel="#harness-alerts",
        hooks_notify_slack_username="Solomon Harness",
        hooks_notify_slack_timeout_s=5.0,
    )
    base.update(overrides)
    return MagicMock(**base)


def _teams_settings(**overrides: object) -> MagicMock:
    """Build a settings mock with Teams defaults; override via kwargs."""
    base = dict(
        hooks_notify_teams_webhook_url="https://outlook.office.com/webhook/abc/def",
        hooks_notify_teams_timeout_s=5.0,
    )
    base.update(overrides)
    return MagicMock(**base)


# === 1. Slack channel ===

class TestSlackChannel:
    @pytest.mark.asyncio
    async def test_slack_channel_posts_to_webhook(self) -> None:
        """Assert POST is made with correct JSON structure."""
        settings = _slack_settings()
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_200()
            await _handle_slack(
                {"message": "deploy ok", "severity": "info"}, settings
            )
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.method == "POST"
            assert req.headers["Content-type"] == "application/json"
            assert req.headers["X-harness-event"] == "Notification"
            body = json.loads(req.data.decode("utf-8"))
            # Required Slack fields.
            assert body["text"] == "deploy ok"
            assert body["username"] == "Solomon Harness"
            assert body["channel"] == "#harness-alerts"
            assert "attachments" in body
            assert len(body["attachments"]) == 1

    @pytest.mark.asyncio
    async def test_slack_severity_to_color_mapping(self) -> None:
        """info→good, warn→warning, error→danger."""
        cases = [("info", "good"), ("warn", "warning"), ("error", "danger")]
        for severity, expected_color in cases:
            assert _severity_to_slack_color(severity) == expected_color, (
                f"{severity} should map to {expected_color}"
            )
        # Unknown severity defaults to "good" (info-like).
        assert _severity_to_slack_color("trace") == "good"
        assert _severity_to_slack_color("") == "good"

    @pytest.mark.asyncio
    async def test_slack_disabled_when_url_empty(self) -> None:
        """When webhook URL is empty, no POST is made."""
        settings = _slack_settings(hooks_notify_slack_webhook_url="")
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen:
            await _handle_slack({"message": "x", "severity": "info"}, settings)
            mock_urlopen.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_payload_contains_severity_message(self) -> None:
        """Payload attachment includes severity + message in fields/text."""
        settings = _slack_settings()
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_200()
            await _handle_slack(
                {"message": "compaction done", "severity": "warn"}, settings
            )
            req = mock_urlopen.call_args[0][0]
            body = json.loads(req.data.decode("utf-8"))
            # Top-level text carries the message.
            assert body["text"] == "compaction done"
            # Attachment color reflects severity.
            assert body["attachments"][0]["color"] == "warning"
            # Fields include Event + Severity.
            fields = {f["title"]: f["value"] for f in body["attachments"][0]["fields"]}
            assert fields["Severity"] == "warn"
            assert fields["Event"] == "Notification"

    @pytest.mark.asyncio
    async def test_slack_http_error_does_not_raise(self) -> None:
        """Slack returning 4xx/5xx is a soft failure — log + skip."""
        import urllib.error

        settings = _slack_settings()
        err = urllib.error.HTTPError(
            "https://hooks.slack.com/services/T0/B1/SECRET",
            500,
            "Server Error",
            {},
            None,
        )
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=err,
        ):
            # Should not raise.
            await _handle_slack({"message": "hi"}, settings)


# === 2. Teams channel ===

class TestTeamsChannel:
    @pytest.mark.asyncio
    async def test_teams_channel_posts_message_card(self) -> None:
        """Assert POST is made with MessageCard format."""
        settings = _teams_settings()
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _mock_urlopen_200()
            await _handle_teams(
                {"message": "merge ok", "severity": "info"}, settings
            )
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            assert req.method == "POST"
            assert req.headers["Content-type"] == "application/json"
            body = json.loads(req.data.decode("utf-8"))
            # MessageCard schema.
            assert body["@type"] == "MessageCard"
            assert body["@context"] == "https://schema.org/extensions"
            assert body["summary"] == "Harness notification"
            assert "sections" in body
            assert len(body["sections"]) == 1
            assert body["sections"][0]["activityTitle"] == "Harness Alert"
            # Body text carries message + severity.
            section_text = body["sections"][0]["text"]
            assert "merge ok" in section_text
            assert "Severity: info" in section_text

    @pytest.mark.asyncio
    async def test_teams_severity_to_theme_color_mapping(self) -> None:
        """info→0078D4, warn→FFA500, error→FF0000."""
        cases = [("info", "0078D4"), ("warn", "FFA500"), ("error", "FF0000")]
        for severity, expected in cases:
            assert _severity_to_teams_color(severity) == expected, (
                f"{severity} should map to {expected}"
            )
        # Unknown severity defaults to info blue.
        assert _severity_to_teams_color("trace") == "0078D4"
        assert _severity_to_teams_color("") == "0078D4"

    @pytest.mark.asyncio
    async def test_teams_disabled_when_url_empty(self) -> None:
        """When webhook URL is empty, no POST is made."""
        settings = _teams_settings(hooks_notify_teams_webhook_url="")
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen:
            await _handle_teams({"message": "x", "severity": "info"}, settings)
            mock_urlopen.assert_not_called()


# === 3. Webhook URL redaction ===

class TestUrlRedaction:
    def test_redact_slack_webhook_url(self) -> None:
        """Slack URL path/query is replaced with *** — host kept."""
        url = "https://hooks.slack.com/services/T0/B1/SECRET_TOKEN"
        redacted = _redact_webhook_url(url)
        assert "SECRET_TOKEN" not in redacted
        assert redacted == "https://hooks.slack.com/***"

    def test_redact_teams_webhook_url(self) -> None:
        url = "https://outlook.office.com/webhook/abc/def?token=xyz"
        redacted = _redact_webhook_url(url)
        assert "xyz" not in redacted
        assert "abc" not in redacted
        assert redacted == "https://outlook.office.com/***"

    def test_redact_empty_url(self) -> None:
        assert _redact_webhook_url("") == "<unset>"

    @pytest.mark.asyncio
    async def test_slack_webhook_url_redacted_in_logs(self) -> None:
        """URL must NOT appear verbatim in log output on HTTP error.

        This is the critical security property: even when Slack returns a
        500, the URL (which contains the secret token) is redacted to
        ``scheme://host/***`` before being written to the log.
        """
        import urllib.error

        secret_url = "https://hooks.slack.com/services/T0/B1/SECRET_TOKEN_42"
        settings = _slack_settings(hooks_notify_slack_webhook_url=secret_url)
        err = urllib.error.HTTPError(secret_url, 500, "Server Error", {}, None)
        # Capture logger output via caplog.
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=err,
        ):
            with patch(
                "harness.hooks.builtin.notify_terminal.logger"
            ) as mock_logger:
                await _handle_slack({"message": "hi"}, settings)
                # A warning was emitted.
                assert mock_logger.warning.called
                # Inspect the formatted message — secret must NOT appear.
                call_args = mock_logger.warning.call_args
                # Combine args for inspection (format string + args).
                fmt = call_args.args[0]
                fmt_args = call_args.args[1:]
                # The URL appears as a format arg (redacted). Check the raw
                # secret never leaks: SECRET_TOKEN_42 must not be in any arg.
                for a in fmt_args:
                    assert "SECRET_TOKEN_42" not in str(a), (
                        "Secret token leaked into log arg"
                    )
                assert "SECRET_TOKEN_42" not in fmt


# === 4. Dispatcher registration ===

class TestDispatcherRegistration:
    @pytest.mark.asyncio
    async def test_handler_table_includes_five_channels(self) -> None:
        assert set(_HANDLERS) == {
            "stdout",
            "webhook",
            "desktop",
            "slack",
            "teams",
        }

    @pytest.mark.asyncio
    async def test_dispatcher_routes_to_slack(self) -> None:
        """Slack channel fires via the public hook entry."""
        from harness.config import Settings

        settings = Settings(hooks_notify_slack_webhook_url="https://hooks.slack.com/x")
        # Patch Settings() inside the hook module to return our mock-friendly instance.
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen, patch(
            "harness.hooks.builtin.notify_terminal.Settings", return_value=settings
        ):
            mock_urlopen.return_value = _mock_urlopen_200()
            ctx = HookContext(
                event="Notification",
                session_id="s1",
                agent_id="",
                payload={
                    "message": "via dispatcher",
                    "severity": "warn",
                    "channels": ["slack"],
                },
            )
            from harness.hooks.builtin.notify_terminal import notify_terminal_hook

            decision = await notify_terminal_hook(ctx)
            assert decision.decision == "allow"
            mock_urlopen.assert_called_once()


# === 5. Settings: 6 new fields ===

class TestSettings:
    def test_slack_webhook_url_default_empty(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_slack_webhook_url == ""

    def test_slack_channel_default_empty(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_slack_channel == ""

    def test_slack_username_default_solomon(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_slack_username == "Solomon Harness"

    def test_slack_timeout_default_5s(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_slack_timeout_s == 5.0

    def test_teams_webhook_url_default_empty(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_teams_webhook_url == ""

    def test_teams_timeout_default_5s(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_teams_timeout_s == 5.0

    def test_all_six_fields_exist(self) -> None:
        from harness.config import Settings

        s = Settings()
        for f in (
            "hooks_notify_slack_webhook_url",
            "hooks_notify_slack_channel",
            "hooks_notify_slack_username",
            "hooks_notify_slack_timeout_s",
            "hooks_notify_teams_webhook_url",
            "hooks_notify_teams_timeout_s",
        ):
            assert hasattr(s, f), f"missing {f}"
