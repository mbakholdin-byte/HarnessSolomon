"""Phase 4.10 Task B: Builtin SecretDetectHook — regex scan for secrets.

PreToolUse defence-in-depth: scans the flattened ``arguments`` string of
a tool call for well-known secret formats (AWS access keys, GitHub PATs,
OpenAI API keys, PEM private keys, JWTs, and ``password=...`` literals).
A match produces a fail-closed ``block`` decision with a human-readable
reason naming the pattern family that fired.

Patterns are intentionally conservative: they trade some recall for
near-zero false positives on standard Python source code. Each pattern
is paired with a short human-readable label used in the block reason.

Trust boundary: stdlib + ``re`` + ``logging`` only. No ``harness.agents``
or ``harness.server`` imports (enforced by ``tests/test_hooks_trust_boundary.py``).
"""
from __future__ import annotations

import logging
import re

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.secret_detect")


# Each entry is (compiled_regex, human_label). Order is stable so the
# FIRST match wins deterministically (lowest index reports the reason).
#
# Patterns follow the published formats of each provider:
#   - AWS IAM access key id: ``AKIA`` + 16 uppercase alphanumerics.
#   - GitHub personal access token (classic): ``ghp_`` + 36 b64 chars.
#   - OpenAI API key (sk-...): ``sk-`` + 48 b64 chars.
#   - PEM private key header (RSA / DSA / EC / OPENSSH / GENERIC).
#   - JWT (compact serialisation): ``eyJ...`` ``.`` ``eyJ...`` ``.`` sig.
#   - ``password=`` / ``password:`` followed by an 8+ char quoted literal.
#
# The ``password`` pattern is case-insensitive (``(?i)`` inline flag) so
# it also catches ``PASSWORD='...'`` / ``Password = "..."``. The 8-char
# minimum suppresses placeholders like ``pw=""`` or ``pwd="x"`` while
# still catching realistic secrets.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"AKIA[0-9A-Z]{16}"),
        "AWS access key",
    ),
    (
        re.compile(r"ghp_[a-zA-Z0-9]{36}"),
        "GitHub personal access token",
    ),
    (
        re.compile(r"sk-[a-zA-Z0-9]{48}"),
        "OpenAI API key",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "PEM private key",
    ),
    (
        re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"),
        "JWT token",
    ),
    (
        re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{8,}"),
        "password literal in arguments",
    ),
)


def _args_to_string(arguments: object) -> str:
    """Flatten tool arguments into a single string for regex scanning.

    Mirrors ``block_dangerous._args_to_string``. Dicts are joined by
    values (not keys — keys like ``command`` / ``content`` would only
    add noise), lists/tuples by elements, everything else stringified.
    """
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        return " ".join(_args_to_string(v) for v in arguments.values())
    if isinstance(arguments, (list, tuple)):
        return " ".join(_args_to_string(v) for v in arguments)
    return str(arguments)


async def secret_detect_hook(context: HookContext) -> HookDecision:
    """Block PreToolUse if any secret pattern matches the tool arguments.

    Non-PreToolUse events pass through (return ``allow``) — secret
    detection only makes sense before a tool runs. An empty / missing
    ``arguments`` field also short-circuits to ``allow``.
    """
    if context.event != "PreToolUse":
        return HookDecision(decision="allow", hook_id="user.builtin.secret_detect")
    arguments = context.payload.get("arguments", {})
    target = _args_to_string(arguments)
    if not target:
        return HookDecision(decision="allow", hook_id="user.builtin.secret_detect")
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(target):
            reason = f"Found {label}"
            logger.warning("SecretDetect: %s", reason)
            return HookDecision(
                decision="block",
                hook_id="user.builtin.secret_detect",
                output={"reason": reason},
            )
    return HookDecision(decision="allow", hook_id="user.builtin.secret_detect")


__all__ = ["secret_detect_hook"]
