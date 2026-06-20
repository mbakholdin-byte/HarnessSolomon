"""Example Harness plugin — structured tool-call logger.

Phase 6.3 v1.28.0 — second reference plugin. Registers an ``OnToolUse``
hook that records each tool invocation with a timestamp + tool name +
session id. Output goes to ``stderr`` in a structured single-line format
so it can be tailed / grepped easily.

Usage
-----

1. Set ``HARNESS_PLUGINS_ENABLED=true``.
2. Place this file in ``.harness/plugins/`` (already shipped here).
3. Optionally whitelist: ``HARNESS_PLUGINS_ALLOWED='["tool_logger"]'``.

Trust boundary: this file imports ONLY stdlib (``sys``, ``time``,
``datetime``). The AST pre-scan in the loader verifies it does not
import ``harness.agents`` or ``harness.server``.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from harness.plugins import PluginRegistry

PLUGIN_NAME = "tool_logger"
PLUGIN_VERSION = "1.0.0"


def _on_tool_use(event: dict[str, Any]) -> dict[str, Any]:
    """Log a tool-use event to stderr with ISO-8601 timestamp.

    Returns a small structured record so that callers (tests /
    dispatchers) can verify the callback ran. The return value is
    purely informational — the dispatcher does not act on it.
    """
    tool = event.get("tool_name", "<unknown>")
    session = event.get("session_id", "<no-session>")
    ts = datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")
    line = f"[tool_logger] ts={ts} tool={tool} session={session}"
    print(line, file=sys.stderr, flush=True)
    return {"logged_at": ts, "tool": tool, "session": session}


def register(registry: PluginRegistry) -> None:
    """Register the OnToolUse structured-logging hook."""
    registry.register_hook(
        "OnToolUse",
        _on_tool_use,
        plugin_name=PLUGIN_NAME,
    )
