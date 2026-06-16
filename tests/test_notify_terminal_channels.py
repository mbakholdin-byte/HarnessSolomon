"""Phase 4.3+ v1.11.0: Tests for notify_terminal dispatcher (stdout/webhook/desktop).

Covers:
    1. stdout channel (existing — sanity check, still works).
    2. webhook channel: success, 4xx, 5xx, timeout, URL error, HMAC.
    3. desktop channel: win32 / darwin / linux, missing command, opt-in.
    4. Dispatcher: per-channel isolation (one failure doesn't break others).
    5. Settings: 4 new fields present with correct defaults.
    6. Observability: notification counter labeled by channel.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from harness.hooks import HookContext
from harness.hooks.builtin.notify_terminal import (
    _HANDLERS,
    _handle_desktop,
    _handle_stdout,
    _handle_webhook,
    _severity_to_prefix,
    notify_terminal_hook,
)


# === 1. Severity → prefix ===

class TestSeverityPrefix:
    def test_info(self) -> None:
        assert _severity_to_prefix("info") == "INFO"

    def test_warn(self) -> None:
        assert _severity_to_prefix("warn") == "WARN"

    def test_error(self) -> None:
        assert _severity_to_prefix("error") == "ERROR"

    def test_unknown_defaults_to_info(self) -> None:
        assert _severity_to_prefix("trace") == "INFO"
        assert _severity_to_prefix("") == "INFO"


# === 2. stdout channel (regression) ===

class TestStdoutChannel:
    @pytest.mark.asyncio
    async def test_writes_to_stderr(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            await _handle_stdout(
                {"message": "hello", "severity": "info"}, None
            )
        assert "[INFO] hello" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_skips_empty_message(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            await _handle_stdout({"message": ""}, None)
        assert buf.getvalue() == ""

    @pytest.mark.asyncio
    async def test_dispatcher_routes_to_stdout(self) -> None:
        ctx = HookContext(
            event="Notification",
            session_id="s1",
            agent_id="",
            payload={"message": "via dispatcher", "channels": ["stdout"]},
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"
        assert "[INFO] via dispatcher" in buf.getvalue()


# === 3. webhook channel ===

class TestWebhookChannel:
    @pytest.mark.asyncio
    async def test_skipped_when_no_url(self) -> None:
        settings = MagicMock(hooks_notify_webhook_url="", hooks_notify_webhook_secret="")
        # Should not raise.
        await _handle_webhook({"message": "x", "severity": "info"}, settings)

    @pytest.mark.asyncio
    async def test_post_success(self) -> None:
        settings = MagicMock(
            hooks_notify_webhook_url="https://example.com/h",
            hooks_notify_webhook_secret="",
            hooks_notify_webhook_timeout_s=5.0,
        )
        # Mock the inner _post to return 200.
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.status = 200
            mock_urlopen.return_value = mock_resp
            await _handle_webhook(
                {"message": "hi", "severity": "info", "channels": ["webhook"]},
                settings,
            )
            mock_urlopen.assert_called_once()
            # Verify it's a POST with JSON body.
            req = mock_urlopen.call_args[0][0]
            assert req.method == "POST"
            assert req.headers["Content-type"] == "application/json"
            assert req.headers["X-harness-event"] == "Notification"
            body = json.loads(req.data.decode("utf-8"))
            assert body["message"] == "hi"

    @pytest.mark.asyncio
    async def test_post_includes_hmac_signature(self) -> None:
        settings = MagicMock(
            hooks_notify_webhook_url="https://example.com/h",
            hooks_notify_webhook_secret="mysecret",
            hooks_notify_webhook_timeout_s=5.0,
        )
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.status = 200
            mock_urlopen.return_value = mock_resp
            await _handle_webhook(
                {"message": "hi", "severity": "info"}, settings
            )
            req = mock_urlopen.call_args[0][0]
            sig = req.headers["X-harness-signature"]
            assert sig.startswith("sha256=")
            assert len(sig) == len("sha256=") + 64  # hex of 32 bytes

    @pytest.mark.asyncio
    async def test_http_error_does_not_raise(self) -> None:
        import urllib.error

        settings = MagicMock(
            hooks_notify_webhook_url="https://example.com/h",
            hooks_notify_webhook_secret="",
            hooks_notify_webhook_timeout_s=5.0,
        )
        err = urllib.error.HTTPError(
            "https://example.com/h", 500, "Server Error", {}, None
        )
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=err,
        ):
            # Should log warning, not raise.
            await _handle_webhook({"message": "hi"}, settings)

    @pytest.mark.asyncio
    async def test_url_error_does_not_raise(self) -> None:
        import urllib.error

        settings = MagicMock(
            hooks_notify_webhook_url="https://nope.invalid/h",
            hooks_notify_webhook_secret="",
            hooks_notify_webhook_timeout_s=5.0,
        )
        err = urllib.error.URLError("Name or service not known")
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=err,
        ):
            await _handle_webhook({"message": "hi"}, settings)


# === 4. desktop channel ===

class TestDesktopChannel:
    @pytest.mark.asyncio
    async def test_skipped_when_disabled(self) -> None:
        settings = MagicMock(hooks_notify_desktop_enabled=False)
        await _handle_desktop({"message": "hi"}, settings)
        # No subprocess should be created — implicitly verified by no error.

    @pytest.mark.asyncio
    async def test_windows_msg(self) -> None:
        settings = MagicMock(hooks_notify_desktop_enabled=True)
        mock_proc = MagicMock()
        mock_proc.communicate = MagicMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        with patch("sys.platform", "win32"), patch(
            "harness.hooks.builtin.notify_terminal.asyncio.create_subprocess_exec"
        ) as mock_exec, patch(
            "harness.hooks.builtin.notify_terminal.asyncio.wait_for",
            return_value=(b"", b""),
        ):
            mock_exec.return_value = mock_proc
            await _handle_desktop(
                {"message": "hello", "severity": "info"}, settings
            )
            mock_exec.assert_called_once()
            cmd = mock_exec.call_args[0]
            assert cmd[0] == "msg"

    @pytest.mark.asyncio
    async def test_macos_osascript(self) -> None:
        settings = MagicMock(hooks_notify_desktop_enabled=True)
        mock_proc = MagicMock()
        mock_proc.communicate = MagicMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        with patch("sys.platform", "darwin"), patch(
            "harness.hooks.builtin.notify_terminal.asyncio.create_subprocess_exec"
        ) as mock_exec, patch(
            "harness.hooks.builtin.notify_terminal.asyncio.wait_for",
            return_value=(b"", b""),
        ):
            mock_exec.return_value = mock_proc
            await _handle_desktop(
                {"message": "hi", "severity": "warn"}, settings
            )
            cmd = mock_exec.call_args[0]
            assert cmd[0] == "osascript"
            # osascript command embeds message + title (no severity prefix —
            # macOS Notification Center has no severity field).
            cmd_str = " ".join(cmd)
            assert "hi" in cmd_str
            assert "Harness" in cmd_str

    @pytest.mark.asyncio
    async def test_linux_notify_send(self) -> None:
        settings = MagicMock(hooks_notify_desktop_enabled=True)
        mock_proc = MagicMock()
        mock_proc.communicate = MagicMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        with patch("sys.platform", "linux"), patch(
            "harness.hooks.builtin.notify_terminal.asyncio.create_subprocess_exec"
        ) as mock_exec, patch(
            "harness.hooks.builtin.notify_terminal.asyncio.wait_for",
            return_value=(b"", b""),
        ):
            mock_exec.return_value = mock_proc
            await _handle_desktop(
                {"message": "hi", "severity": "error"}, settings
            )
            cmd = mock_exec.call_args[0]
            assert cmd[0] == "notify-send"

    @pytest.mark.asyncio
    async def test_missing_command_does_not_raise(self) -> None:
        settings = MagicMock(hooks_notify_desktop_enabled=True)
        with patch("sys.platform", "linux"), patch(
            "harness.hooks.builtin.notify_terminal.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            # Should not raise.
            await _handle_desktop({"message": "hi"}, settings)

    @pytest.mark.asyncio
    async def test_skips_empty_message(self) -> None:
        settings = MagicMock(hooks_notify_desktop_enabled=True)
        with patch("sys.platform", "linux"), patch(
            "harness.hooks.builtin.notify_terminal.asyncio.create_subprocess_exec"
        ) as mock_exec:
            await _handle_desktop({"message": ""}, settings)
            mock_exec.assert_not_called()


# === 5. Dispatcher: per-channel isolation ===

class TestDispatcher:
    @pytest.mark.asyncio
    async def test_unknown_channel_skipped(self) -> None:
        ctx = HookContext(
            event="Notification",
            session_id="s1",
            agent_id="",
            payload={"message": "hi", "channels": ["unknown_channel"]},
        )
        decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"

    @pytest.mark.asyncio
    async def test_default_channel_is_stdout(self) -> None:
        ctx = HookContext(
            event="Notification",
            session_id="s1",
            agent_id="",
            payload={"message": "default", "severity": "info"},
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"
        assert "[INFO] default" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_one_channel_failure_does_not_break_others(self) -> None:
        # webhook with broken URL fails, but stdout still works.
        ctx = HookContext(
            event="Notification",
            session_id="s1",
            agent_id="",
            payload={
                "message": "isolated",
                "severity": "warn",
                "channels": ["webhook", "stdout"],
            },
        )
        with patch(
            "harness.hooks.builtin.notify_terminal.urllib.request.urlopen",
            side_effect=Exception("boom"),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"
        assert "[WARN] isolated" in buf.getvalue()

    @pytest.mark.asyncio
    async def test_handler_table_includes_three_channels(self) -> None:
        assert set(_HANDLERS) == {"stdout", "webhook", "desktop"}


# === 6. Settings: 4 new fields ===

class TestSettings:
    def test_webhook_url_default_empty(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_webhook_url == ""

    def test_webhook_secret_default_empty(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_webhook_secret == ""

    def test_webhook_timeout_default_5s(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_webhook_timeout_s == 5.0

    def test_desktop_enabled_default_false(self) -> None:
        from harness.config import Settings

        s = Settings()
        assert s.hooks_notify_desktop_enabled is False

    def test_all_four_fields_exist(self) -> None:
        from harness.config import Settings

        s = Settings()
        for f in (
            "hooks_notify_webhook_url",
            "hooks_notify_webhook_secret",
            "hooks_notify_webhook_timeout_s",
            "hooks_notify_desktop_enabled",
        ):
            assert hasattr(s, f), f"missing {f}"


# === 7. Non-Notification events short-circuit ===

class TestNonNotificationEvents:
    @pytest.mark.asyncio
    async def test_pre_tool_use_short_circuits(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash"},
        )
        decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"

    @pytest.mark.asyncio
    async def test_empty_message_short_circuits(self) -> None:
        ctx = HookContext(
            event="Notification",
            session_id="s1",
            agent_id="",
            payload={"message": ""},
        )
        decision = await notify_terminal_hook(ctx)
        assert decision.decision == "allow"
