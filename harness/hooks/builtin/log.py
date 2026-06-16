"""Phase 4.0: Builtin LogHook — emits a structured log line per event.

Default ON. Logs at INFO level via the standard ``logging`` module
under the ``harness.hooks.builtin`` logger namespace. Hooks are
read-only and never block.
"""
from __future__ import annotations

import logging

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.log")


async def log_hook(context: HookContext) -> HookDecision:
    """Emit a single INFO log line for ``context.event``."""
    tool_name = context.payload.get("tool_name", "")
    session = context.session_id or "-"
    logger.info(
        "hook event=%s session=%s agent=%s tool=%s req=%s depth=%d",
        context.event,
        session,
        context.agent_id or "-",
        tool_name or "-",
        context.request_id or "-",
        context.recursion_depth,
    )
    return HookDecision(decision="allow", hook_id="builtin.log")
