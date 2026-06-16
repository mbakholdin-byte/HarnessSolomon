"""Phase 4.3+ v1.11.0: Builtin NotifyTerminalHook — multi-channel Notification fanout.

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

Payload contract::

    {
        "severity": "info" | "warn" | "error",  # default: "info"
        "message":  "Compaction completed in 1.2s",  # required, non-empty
        "channels": ["stdout", "webhook", "desktop"],  # default: ["stdout"]
    }

Each channel is a separate handler. Failures are isolated: a 500
from the webhook does not prevent the desktop notification from
firing. All handlers are fail-open (any exception → log + skip).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sys
import urllib.error
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
}


# === Public hook entry point ===

async def notify_terminal_hook(context: HookContext) -> HookDecision:
    """Forward Notification events to stderr (stdout channel).

    Phase 4.3+ v1.11.0: now also dispatches to ``webhook`` and
    ``desktop`` channels when present in ``payload["channels"]``.
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
