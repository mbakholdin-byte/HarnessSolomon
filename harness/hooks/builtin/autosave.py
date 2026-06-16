"""Phase 4.0: Builtin AutosaveHook — persist session state on SessionEnd.

Default ON. Fires on ``SessionEnd`` and writes a session summary
to L4 (file adapter) via the scratchpad audit log. The hook is
read-only on the in-memory state; it only writes an audit line.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.autosave")


def _audit_dir() -> Path:
    """Return the audit directory (created on demand)."""
    p = Path("data") / "audit"
    p.mkdir(parents=True, exist_ok=True)
    return p


async def autosave_hook(context: HookContext) -> HookDecision:
    """On SessionEnd, append a JSONL audit line summarising the session."""
    if context.event != "SessionEnd":
        return HookDecision(decision="allow", hook_id="builtin.autosave")
    try:
        line = json.dumps(
            {
                "event": context.event,
                "session_id": context.session_id,
                "agent_id": context.agent_id,
                "ts": time.time(),
                "payload": context.payload,
            },
            ensure_ascii=False,
        )
        path = _audit_dir() / "session-end.ndjson"
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        # Autosave is best-effort; never block on failure.
        logger.warning("Autosave: %s: %s", type(e).__name__, e)
    return HookDecision(decision="allow", hook_id="builtin.autosave")
