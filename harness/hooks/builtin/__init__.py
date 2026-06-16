"""Phase 4.0: Builtin hooks.

Five builtin hooks (Plan § 5):

    - ``log`` (default ON) — emit a structured INFO log per event.
    - ``validate`` (default ON) — Pydantic schema gate for tool args.
    - ``block_dangerous`` (default ON) — regex denylist, fail-closed.
    - ``inject_context`` (default OFF) — prepend L0/L1 to UserPromptSubmit.
    - ``autosave`` (default ON) — SessionEnd → NDJSON audit line.

Each is a public async function ``hook(context) -> HookDecision``.

Test code may import individual hooks from this subpackage
(``from harness.hooks.builtin import log_hook``) — production code
goes through the registry (no direct import).
"""
from __future__ import annotations

from harness.hooks.builtin.autosave import autosave_hook
from harness.hooks.builtin.block_dangerous import block_dangerous_hook
from harness.hooks.builtin.inject_context import inject_context_hook
from harness.hooks.builtin.log import log_hook
from harness.hooks.builtin.validate import validate_hook

BUILTIN_HOOKS: dict[str, object] = {
    "log": log_hook,
    "validate": validate_hook,
    "block_dangerous": block_dangerous_hook,
    "inject_context": inject_context_hook,
    "autosave": autosave_hook,
}

__all__ = [
    "log_hook",
    "validate_hook",
    "block_dangerous_hook",
    "inject_context_hook",
    "autosave_hook",
    "BUILTIN_HOOKS",
]
