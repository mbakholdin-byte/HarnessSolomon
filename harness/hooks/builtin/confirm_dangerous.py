"""Phase 4.3: Builtin ConfirmDangerousHook — Elicitation for risk gates.

Default ON (when Elicitation event is enabled). Listens to
``Elicitation`` events whose payload carries a ``tool_name`` + a
flag ``requires_confirmation=True``. The hook injects a default
``"proceed"`` answer (so the agent loop never deadlocks waiting
for a human) and emits ``emit_elicitation_response`` for
observability.

Payload contract::

    {
        "question": "Run rm -rf /tmp/foo? [y/N]",
        "options": ["proceed", "abort"],
        "multi_select": False,
        "default_answer": "abort",
        "tool_name": "bash",                # optional, for matching
        "requires_confirmation": True,      # gate flag
    }

Decision shape:
    - ``modify`` → answer replaced with ``default_answer`` (we never
      block the agent — fail-open on Elicitation to keep the loop alive).
    - ``allow``  → answer is fine, no override.
    - ``block``  → reserved (we don't use it — agent loops should not
      hard-block on Elicitation).

Why fail-open: an Elicitation hook that returns ``block`` would
stop the agent loop entirely. The user can still gate dangerous
actions through ``PreToolUse:BlockDangerous`` (fail-closed at
the tool layer) and via the existing perms denylist. Elicitation
is the *interactive* layer; if no human is around, the default
answer is the safe choice (typically ``abort``).
"""
from __future__ import annotations

import logging

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.confirm_dangerous")


async def confirm_dangerous_hook(context: HookContext) -> HookDecision:
    """Inject a default answer for Elicitation prompts that need confirmation.

    The default behaviour (fail-open with conservative default) keeps
    the agent loop alive: a missing human is NOT a hard stop, it's
    a "use the default answer" decision.
    """
    if context.event != "Elicitation":
        return HookDecision(decision="allow", hook_id="builtin.confirm_dangerous")
    payload = context.payload
    if not payload.get("requires_confirmation"):
        # Not a confirmation-gated prompt — let other hooks decide.
        return HookDecision(decision="allow", hook_id="builtin.confirm_dangerous")
    default = payload.get("default_answer", "abort")
    new_payload = dict(payload)
    new_payload["answer"] = default
    new_payload["answer_source"] = "builtin.confirm_dangerous"
    logger.debug(
        "ConfirmDangerous: overriding answer to %r for question %r",
        default,
        payload.get("question", "")[:80],
    )
    return HookDecision(
        decision="modify",
        hook_id="builtin.confirm_dangerous",
        output={"payload": new_payload},
    )
