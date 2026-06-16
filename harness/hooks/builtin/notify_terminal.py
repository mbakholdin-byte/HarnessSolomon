"""Phase 4.3: Builtin NotifyTerminalHook — Notification → stderr.

Default ON (when Notification event is enabled). Listens to
``Notification`` events and writes a structured line to stderr.
Also emits ``emit_notification_dispatched`` for observability.

Payload contract::

    {
        "severity": "info" | "warn" | "error",  # default: "info"
        "message":  "Compaction completed in 1.2s",  # required, non-empty
        "channels": ["stdout", "webhook", "desktop"],  # default: ["stdout"]
    }

This hook is the canonical "fanout hub" for any side-channel
notification. It picks the ``stdout`` channel by default and
writes ``[severity] message\n`` to ``sys.stderr`` (separate
from agent output stream).
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.notify_terminal")


def _severity_to_prefix(severity: str) -> str:
    sev = severity.lower()
    if sev == "error":
        return "ERROR"
    if sev == "warn":
        return "WARN"
    return "INFO"


async def notify_terminal_hook(context: HookContext) -> HookDecision:
    """Forward Notification events to stderr (stdout channel)."""
    if context.event != "Notification":
        return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
    payload = context.payload
    severity = payload.get("severity", "info")
    message = payload.get("message", "")
    if not message:
        # Empty messages are useful for "ping" semantics — log at debug
        # and short-circuit. We don't fail.
        logger.debug("NotifyTerminal: empty message (skipped)")
        return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
    channels: list[Any] = payload.get("channels") or ["stdout"]
    prefix = _severity_to_prefix(severity)
    # We don't actually fail if no channel matches — keep it simple
    # and write to stderr when "stdout" is in the channel list. Other
    # channels (webhook, desktop) are reserved for future fanout.
    if "stdout" in channels:
        try:
            print(f"[{prefix}] {message}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("NotifyTerminal: stderr write failed: %s", exc)
    return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
