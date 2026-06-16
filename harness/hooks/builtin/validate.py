"""Phase 4.0: Builtin ValidateHook — Pydantic schema gate for tool args.

Default ON. For ``PreToolUse`` events, validates the ``arguments``
field of the payload against the tool's Pydantic input model (if
the tool has one registered in ``TOOL_SCHEMAS``). Blocks with a
descriptive reason on validation failure.

This is a Tier 1 sink — runs before the tool is invoked. A failed
validation here is an early exit (block decision propagates to
the runner which returns ``block``).
"""
from __future__ import annotations

import logging

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.validate")


# Test override slot. Tests can ``monkeypatch.setitem`` into this
# dict to register schemas without depending on a real
# ``harness.tools.schemas`` module. Production usage leaves it empty.
_SCHEMAS_OVERRIDE: dict[str, type] = {}


# Tools with their Pydantic input models are discovered from
# ``harness.tools.schemas.TOOL_SCHEMAS_BY_NAME`` (lazy import to
# avoid circular imports and respect the trust boundary direction:
# hooks import from tools, not the reverse).
def _get_tool_schemas() -> dict[str, type]:
    """Return the registry of tool_name → Pydantic model. Empty if not loaded."""
    try:
        from harness.tools.schemas import TOOL_SCHEMAS_BY_NAME

        return dict(TOOL_SCHEMAS_BY_NAME)
    except ImportError:
        pass
    return dict(_SCHEMAS_OVERRIDE)


async def validate_hook(context: HookContext) -> HookDecision:
    """Block PreToolUse if ``arguments`` don't match the tool's Pydantic model."""
    if context.event != "PreToolUse":
        return HookDecision(decision="allow", hook_id="builtin.validate")
    tool_name = context.payload.get("tool_name", "")
    if not tool_name:
        return HookDecision(decision="allow", hook_id="builtin.validate")
    arguments = context.payload.get("arguments", {})
    if not isinstance(arguments, dict):
        return HookDecision(
            decision="block",
            hook_id="builtin.validate",
            output={"reason": f"arguments must be a dict, got {type(arguments).__name__}"},
        )
    schemas = _get_tool_schemas()
    model = schemas.get(tool_name)
    if model is None:
        # No schema registered → nothing to validate.
        return HookDecision(decision="allow", hook_id="builtin.validate")
    try:
        model.model_validate(arguments)
    except Exception as e:  # noqa: BLE001 — Pydantic ValidationError is broad
        reason = f"validation failed for {tool_name}: {e!r}"
        logger.warning("Built-in validate: %s", reason)
        return HookDecision(
            decision="block",
            hook_id="builtin.validate",
            output={"reason": reason[:500]},
        )
    return HookDecision(decision="allow", hook_id="builtin.validate")
