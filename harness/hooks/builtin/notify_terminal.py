"""Phase 4.3+ v1.16.0: Builtin NotifyTerminalHook — multi-channel Notification fanout.

Default ON. Listens to ``Notification`` events and dispatches the
payload to one or more channels:

    - ``stdout``  → write ``[severity] message`` to stderr (the
      canonical side-channel; never collides with agent output).
    - ``webhook`` → HTTP POST to ``settings.hooks_notify_webhook_url``
      with HMAC-SHA256 signature header.
    - ``desktop`` → platform-specific toast/notification command
      (Windows: PowerShell BurntToast or ``msg *`` fallback,
      macOS: ``osascript``, Linux: ``notify-send``). Opt-in via
      ``settings.hooks_notify_desktop_enabled``.
    - ``slack``   → POST a Slack incoming-webhook payload to
      ``settings.hooks_notify_slack_webhook_url``. Severity maps to
      an attachment color (info=green, warn=yellow, error=red). No
      HMAC — the webhook URL itself is the secret (Phase 4.6 v1.16.0).
    - ``teams``   → POST a Microsoft Teams MessageCard to
      ``settings.hooks_notify_teams_webhook_url``. Severity maps to
      ``themeColor`` (info=blue, warn=orange, error=red). No HMAC
      (Phase 4.6 v1.16.0).

Payload contract::

    {
        "severity": "info" | "warn" | "error",  # default: "info"
        "message":  "Compaction completed in 1.2s",  # required, non-empty
        "channels": ["stdout", "webhook", "desktop", "slack", "teams"],  # default: ["stdout"]
    }

Each channel is a separate handler. Failures are isolated: a 500
from the webhook does not prevent the desktop notification from
firing. All handlers are fail-open (any exception → log + skip).

Webhook URLs (Slack/Teams/generic) are NEVER logged. Error messages
reference the env var NAME, never the URL value — mirrors the
``webhook_secret`` policy at ``config.py``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from harness.config import Settings
from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.notify_terminal")


# === Severity → prefix mapping ===

def _severity_to_prefix(severity: str) -> str:
    sev = severity.lower()
    if sev == "error":
        return "ERROR"
    if sev == "warn":
        return "WARN"
    return "INFO"


# === Channel handlers ===
#
# Each handler is ``async def handler(payload: dict, settings) -> None``.
# Failures are caught at the dispatcher level; handlers should
# raise on hard failures (e.g. webhook returns 4xx) and the
# dispatcher logs + continues.


async def _handle_stdout(payload: dict[str, Any], _settings: Any) -> None:
    """Write ``[severity] message`` to stderr."""
    severity = payload.get("severity", "info")
    message = payload.get("message", "")
    if not message:
        return
    prefix = _severity_to_prefix(severity)
    print(f"[{prefix}] {message}", file=sys.stderr, flush=True)


async def _handle_webhook(payload: dict[str, Any], settings: Any) -> None:
    """POST payload to ``settings.hooks_notify_webhook_url`` with HMAC signature.

    Headers:
        Content-Type: application/json
        X-Harness-Signature: sha256=<hex>
        X-Harness-Event: Notification

    On HTTP error: log warning + raise (dispatcher catches + counts).
    """
    url = getattr(settings, "hooks_notify_webhook_url", "")
    if not url:
        logger.debug("NotifyTerminal: webhook channel skipped (no URL configured)")
        return
    secret = getattr(settings, "hooks_notify_webhook_secret", "")
    timeout_s = float(getattr(settings, "hooks_notify_webhook_timeout_s", 5.0))
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Harness-Event": "Notification",
    }
    if secret:
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Harness-Signature"] = f"sha256={sig}"

    def _post() -> int:
        req = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status

    try:
        status = await asyncio.to_thread(_post)
        if status >= 400:
            logger.warning(
                "NotifyTerminal: webhook returned HTTP %d for %s", status, url
            )
            # Do not raise — webhook returning 4xx/5xx is a soft failure.
    except urllib.error.HTTPError as e:
        logger.warning("NotifyTerminal: webhook HTTP %d: %s", e.code, e.reason)
    except urllib.error.URLError as e:
        logger.warning("NotifyTerminal: webhook URL error: %s", e.reason)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("NotifyTerminal: webhook timeout after %.1fs", timeout_s)
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning(
            "NotifyTerminal: webhook unexpected error (%s): %s",
            type(e).__name__,
            e,
        )


# === Severity → color mapping (Slack + Teams) ===
#
# Slack attachment colors:
#   - "good"    → green (info)
#   - "warning" → yellow (warn)
#   - "danger"  → red (error)
# Teams themeColor is a hex string (no leading #):
#   - "0078D4" → blue (info — Teams default accent)
#   - "FFA500" → orange (warn)
#   - "FF0000" → red (error)

_SLACK_COLOR_MAP: dict[str, str] = {
    "info": "good",
    "warn": "warning",
    "error": "danger",
}

_TEAMS_COLOR_MAP: dict[str, str] = {
    "info": "0078D4",
    "warn": "FFA500",
    "error": "FF0000",
}


def _severity_to_slack_color(severity: str) -> str:
    """Map severity to Slack attachment color (good/warning/danger)."""
    return _SLACK_COLOR_MAP.get(severity.lower(), "good")


def _severity_to_teams_color(severity: str) -> str:
    """Map severity to Teams themeColor hex (no leading #)."""
    return _TEAMS_COLOR_MAP.get(severity.lower(), "0078D4")


def _redact_webhook_url(url: str) -> str:
    """Return a redacted form of a webhook URL for safe logging.

    Keeps scheme + host, replaces the path/query/fragment with ``***``.
    Example::

        >>> _redact_webhook_url("https://hooks.slack.com/services/T0/B1/secret")
        'https://hooks.slack.com/***'

        >>> _redact_webhook_url("https://outlook.office.com/webhook/abc/def")
        'https://outlook.office.com/***'

    Empty input returns ``"<unset>"`` for clarity in log lines.
    """
    if not url:
        return "<unset>"
    # urllib.parse to be robust against malformed URLs.
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        # Not a URL — redact entirely (defensive, should not happen).
        return "***"
    return f"{parsed.scheme}://{parsed.netloc}/***"


async def _handle_slack(payload: dict[str, Any], settings: Any) -> None:
    """POST a Slack incoming-webhook payload.

    Payload format::

        {
            "channel": "#harness-alerts",  # optional — from settings
            "username": "Solomon Harness",
            "text": "<message>",
            "attachments": [
                {
                    "color": "good" | "warning" | "danger",
                    "fields": [
                        {"title": "Event", "value": "Notification"},
                        {"title": "Severity", "value": "<severity>"},
                    ],
                }
            ],
        }

    On HTTP error: log warning (URL redacted) and return (fail-open).
    No HMAC — the webhook URL is the secret (Slack convention).
    """
    url = getattr(settings, "hooks_notify_slack_webhook_url", "")
    if not url:
        logger.debug("NotifyTerminal: slack channel skipped (no URL configured)")
        return
    severity = payload.get("severity", "info")
    message = payload.get("message", "")
    channel = getattr(settings, "hooks_notify_slack_channel", "")
    username = getattr(settings, "hooks_notify_slack_username", "Solomon Harness")
    timeout_s = float(getattr(settings, "hooks_notify_slack_timeout_s", 5.0))
    color = _severity_to_slack_color(severity)
    slack_payload: dict[str, Any] = {
        "username": username,
        "text": message,
        "attachments": [
            {
                "color": color,
                "fields": [
                    {"title": "Event", "value": "Notification"},
                    {"title": "Severity", "value": severity},
                ],
            }
        ],
    }
    if channel:
        slack_payload["channel"] = channel
    body = json.dumps(slack_payload, sort_keys=True).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Harness-Event": "Notification",
    }

    def _post() -> int:
        req = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status

    try:
        status = await asyncio.to_thread(_post)
        if status >= 400:
            logger.warning(
                "NotifyTerminal: slack returned HTTP %d for %s",
                status,
                _redact_webhook_url(url),
            )
    except urllib.error.HTTPError as e:
        logger.warning(
            "NotifyTerminal: slack HTTP %d: %s (url=%s)",
            e.code,
            e.reason,
            _redact_webhook_url(url),
        )
    except urllib.error.URLError as e:
        logger.warning(
            "NotifyTerminal: slack URL error: %s (url=%s)",
            e.reason,
            _redact_webhook_url(url),
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "NotifyTerminal: slack timeout after %.1fs (url=%s)",
            timeout_s,
            _redact_webhook_url(url),
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning(
            "NotifyTerminal: slack unexpected error (%s): %s (url=%s)",
            type(e).__name__,
            e,
            _redact_webhook_url(url),
        )


async def _handle_teams(payload: dict[str, Any], settings: Any) -> None:
    """POST a Microsoft Teams MessageCard payload.

    Payload format (Office 365 connector MessageCard)::

        {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": "0078D4" | "FFA500" | "FF0000",
            "summary": "Harness notification",
            "sections": [
                {
                    "activityTitle": "Harness Alert",
                    "text": "Event: Notification  Severity: <severity>\\n<message>",
                }
            ],
        }

    On HTTP error: log warning (URL redacted) and return (fail-open).
    No HMAC — the webhook URL is the secret (Teams convention).
    """
    url = getattr(settings, "hooks_notify_teams_webhook_url", "")
    if not url:
        logger.debug("NotifyTerminal: teams channel skipped (no URL configured)")
        return
    severity = payload.get("severity", "info")
    message = payload.get("message", "")
    timeout_s = float(getattr(settings, "hooks_notify_teams_timeout_s", 5.0))
    theme_color = _severity_to_teams_color(severity)
    teams_payload: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": theme_color,
        "summary": "Harness notification",
        "sections": [
            {
                "activityTitle": "Harness Alert",
                "text": f"Event: Notification  Severity: {severity}\n{message}",
            }
        ],
    }
    body = json.dumps(teams_payload, sort_keys=True).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Harness-Event": "Notification",
    }

    def _post() -> int:
        req = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status

    try:
        status = await asyncio.to_thread(_post)
        if status >= 400:
            logger.warning(
                "NotifyTerminal: teams returned HTTP %d for %s",
                status,
                _redact_webhook_url(url),
            )
    except urllib.error.HTTPError as e:
        logger.warning(
            "NotifyTerminal: teams HTTP %d: %s (url=%s)",
            e.code,
            e.reason,
            _redact_webhook_url(url),
        )
    except urllib.error.URLError as e:
        logger.warning(
            "NotifyTerminal: teams URL error: %s (url=%s)",
            e.reason,
            _redact_webhook_url(url),
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "NotifyTerminal: teams timeout after %.1fs (url=%s)",
            timeout_s,
            _redact_webhook_url(url),
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning(
            "NotifyTerminal: teams unexpected error (%s): %s (url=%s)",
            type(e).__name__,
            e,
            _redact_webhook_url(url),
        )


async def _handle_desktop(payload: dict[str, Any], settings: Any) -> None:
    """Platform-specific desktop notification.

    Windows → PowerShell BurntToast (with ``msg *`` fallback).
    macOS   → ``osascript -e 'display notification ...'``.
    Linux   → ``notify-send``.

    Each subprocess is launched via ``asyncio.create_subprocess_exec``.
    If the command is missing (e.g. notify-send not installed on Linux),
    log a warning and return.
    """
    if not getattr(settings, "hooks_notify_desktop_enabled", False):
        return
    message = payload.get("message", "")
    severity = payload.get("severity", "info")
    if not message:
        return
    title = "Harness"
    platform = sys.platform
    cmd: list[str]
    if platform == "win32":
        # Try BurntToast first; fall back to `msg *` (always present on Windows).
        # We use `msg *` because BurntToast requires a PowerShell module that
        # may not be installed. `msg` shows a popup dialog.
        # Note: `msg` is interactive and may not work over SSH. Best-effort.
        cmd = ["msg", "*", f"[{_severity_to_prefix(severity)}] {message}"]
    elif platform == "darwin":
        # Escape double quotes in message for osascript.
        safe_msg = message.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        cmd = [
            "osascript",
            "-e",
            f'display notification "{safe_msg}" with title "{safe_title}"',
        ]
    else:
        # Linux + others.
        safe_msg = message.replace('"', '\\"')
        cmd = ["notify-send", "-a", title, f"[{_severity_to_prefix(severity)}] {safe_msg}"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=3.0
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("NotifyTerminal: desktop command timed out: %s", cmd[0])
            return
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            logger.debug(
                "NotifyTerminal: desktop command exit %d (%s): %s",
                proc.returncode, cmd[0], err[:200],
            )
    except FileNotFoundError:
        # notify-send / osascript / msg not installed.
        logger.debug(
            "NotifyTerminal: desktop command not found: %s (channel silently skipped)",
            cmd[0] if cmd else "?",
        )
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning(
            "NotifyTerminal: desktop unexpected error (%s): %s",
            type(e).__name__,
            e,
        )


# Channel dispatch table.
_HANDLERS: dict[str, Any] = {
    "stdout": _handle_stdout,
    "webhook": _handle_webhook,
    "desktop": _handle_desktop,
    "slack": _handle_slack,
    "teams": _handle_teams,
}


# === Public hook entry point ===

async def notify_terminal_hook(context: HookContext) -> HookDecision:
    """Forward Notification events to stderr (stdout channel).

    Phase 4.3+ v1.11.0: now also dispatches to ``webhook`` and
    ``desktop`` channels when present in ``payload["channels"]``.

    Phase 4.6 v1.16.0: added ``slack`` and ``teams`` channels. Both
    are webhook-based (no HMAC) and redact the URL in all log lines.
    """
    if context.event != "Notification":
        return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
    payload = context.payload
    message = payload.get("message", "")
    if not message:
        # Empty messages are useful for "ping" semantics — log at debug
        # and short-circuit. We don't fail.
        logger.debug("NotifyTerminal: empty message (skipped)")
        return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
    channels: list[Any] = payload.get("channels") or ["stdout"]
    settings = Settings()
    for ch in channels:
        handler = _HANDLERS.get(ch)
        if handler is None:
            logger.debug("NotifyTerminal: unknown channel %r (skipped)", ch)
            continue
        try:
            await handler(payload, settings)
        except Exception as e:  # noqa: BLE001 — fail-open per channel
            logger.warning(
                "NotifyTerminal: channel %r handler failed (%s): %s",
                ch, type(e).__name__, e,
            )
    return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
