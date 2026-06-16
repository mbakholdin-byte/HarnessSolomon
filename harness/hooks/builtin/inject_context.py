"""Phase 4.0: Builtin InjectContextHook — prepend L0/L1 snapshot.

Default OFF. Phase 3 v1.2.1 already auto-injects L0 via the
``scratchpad_inject_l0_to_system_prompt`` setting; this builtin is
a re-implementation via the hooks framework, useful when the L0
injection is disabled at the runner level but the user still wants
per-event context injection.

For ``UserPromptSubmit`` events, prepends a ``[Harness Context]``
system note with the current scratchpad L0 section. Decision is
``modify`` with the new payload.
"""
from __future__ import annotations

import logging
from typing import Any

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.inject_context")


# L0 source is the scratchpad store (Phase 3 v1.2.0). Lazy import to
# respect the trust boundary direction (hooks read from scratchpad,
# scratchpad does not depend on hooks).
def _get_l0_section(session_id: str) -> str:
    """Return the L0 scratchpad section for ``session_id``, or '' if none."""
    if not session_id:
        return ""
    try:
        from harness.scratchpad.store import ScratchpadStore

        store = ScratchpadStore.from_settings()
        return store.read_l0(session_id)
    except Exception:  # noqa: BLE001
        return ""


async def inject_context_hook(context: HookContext) -> HookDecision:
    """For UserPromptSubmit, prepend a system note with the L0 snapshot."""
    if context.event != "UserPromptSubmit":
        return HookDecision(decision="allow", hook_id="builtin.inject_context")
    l0 = _get_l0_section(context.session_id)
    if not l0:
        return HookDecision(decision="allow", hook_id="builtin.inject_context")
    # Build the new payload: prepend the L0 note to the user prompt.
    new_payload: dict[str, Any] = dict(context.payload)
    original = new_payload.get("prompt", "")
    new_payload["prompt"] = f"[Harness Context]\n{l0}\n\n---\n\n{original}"
    return HookDecision(
        decision="modify",
        hook_id="builtin.inject_context",
        output={"payload": new_payload},
    )
