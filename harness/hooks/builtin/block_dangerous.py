"""Phase 4.0: Builtin BlockDangerousHook — regex denylist for tool args.

Default ON. Defence-in-depth layer on top of the existing perms
denylist in ``harness/agents/runner.py``. Catches well-known
destructive patterns (``rm -rf /``, ``mkfs``, ``DROP DATABASE``,
etc.) and blocks the tool call before it reaches the agent.

This is a fail-closed hook by design: a match means a very
strong signal of intent to do harm, so the decision is ``block``
even if the runner is in fail-open mode.
"""
from __future__ import annotations

import logging
import re

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.block_dangerous")


# Conservative patterns. Each is a case-insensitive regex applied
# to the joined arguments string.
_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"rm\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*(?:[fF][a-zA-Z]*)?\s+/",
            re.IGNORECASE,
        ),
        "rm -r[f] /<path>",
    ),
    (
        re.compile(r"mkfs(?:\.\w+)?\s+/dev/", re.IGNORECASE),
        "mkfs /dev/...",
    ),
    (
        re.compile(r"dd\s+if=.*\s+of=/dev/(?:sd|hd|nvme|vd)", re.IGNORECASE),
        "dd of=/dev/...",
    ),
    (re.compile(r":\(\)\s*\{.*:\|:&.*\};:", re.IGNORECASE), "fork bomb"),
    (re.compile(r"DROP\s+(?:DATABASE|SCHEMA)\b", re.IGNORECASE), "DROP DATABASE"),
    (re.compile(r"TRUNCATE\s+TABLE\s+\w+\s*;", re.IGNORECASE), "TRUNCATE TABLE"),
    (re.compile(r"format\s+c:", re.IGNORECASE), "format c:"),
)


def _args_to_string(arguments: object) -> str:
    """Flatten arguments to a string for pattern matching."""
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        parts: list[str] = []
        for v in arguments.values():
            parts.append(_args_to_string(v))
        return " ".join(parts)
    if isinstance(arguments, (list, tuple)):
        return " ".join(_args_to_string(v) for v in arguments)
    return str(arguments)


async def block_dangerous_hook(context: HookContext) -> HookDecision:
    """Block PreToolUse if arguments match a dangerous pattern."""
    if context.event != "PreToolUse":
        return HookDecision(decision="allow", hook_id="builtin.block_dangerous")
    arguments = context.payload.get("arguments", {})
    target = _args_to_string(arguments)
    for pattern, label in _DANGEROUS_PATTERNS:
        if pattern.search(target):
            reason = f"dangerous pattern matched: {label}"
            logger.warning("BlockDangerous: %s", reason)
            return HookDecision(
                decision="block",
                hook_id="builtin.block_dangerous",
                output={"reason": reason},
            )
    return HookDecision(decision="allow", hook_id="builtin.block_dangerous")
