"""Example Harness plugin — stderr logger.

Phase 6.2A v1.27.0 — reference plugin showing the minimal registration
contract. Registers an ``OnToolUse`` hook that prints each tool-use
event to ``stderr``. Useful for local debugging: enable by setting
``HARNESS_PLUGINS_ENABLED=true`` and placing this file in
``.harness/plugins/``.

This file intentionally imports ONLY stdlib (``sys``) — the AST trust-
boundary test verifies it does not import ``harness.agents`` /
``harness.server``.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from harness.plugins import PluginRegistry

PLUGIN_NAME = "example_logger"
PLUGIN_VERSION = "1.0.0"


def _on_tool_use(event: dict[str, Any]) -> None:
    """Print a one-line summary of the tool-use event to stderr."""
    tool = event.get("tool_name", "<unknown>")
    session = event.get("session_id", "<no-session>")
    print(
        f"[example_logger] tool={tool} session={session}",
        file=sys.stderr,
        flush=True,
    )


def register(registry: PluginRegistry) -> None:
    """Register the OnToolUse stderr-logging hook."""
    registry.register_hook(
        "OnToolUse",
        _on_tool_use,
        plugin_name=PLUGIN_NAME,
    )
