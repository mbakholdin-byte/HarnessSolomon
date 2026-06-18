"""Phase 4.0 + 4.3 + 4.10: Builtin hooks.

Ten builtin hooks (Plan § 5 + Phase 4.3 + Phase 4.10):

    - ``log`` (default ON) — emit a structured INFO log per event.
    - ``validate`` (default ON) — Pydantic schema gate for tool args.
    - ``block_dangerous`` (default ON) — regex denylist, fail-closed.
    - ``inject_context`` (default OFF) — prepend L0/L1 to UserPromptSubmit.
    - ``autosave`` (default ON) — SessionEnd → NDJSON audit line.
    - ``confirm_dangerous`` (default ON, Phase 4.3) — Elicitation
      gate that injects a safe default answer.
    - ``notify_terminal`` (default ON, Phase 4.3) — Notification
      fanout to stderr (stdout channel).
    - ``license_check`` (default ON, Phase 4.10 Task A) — advisory
      license header / SPDX checker.
    - ``complexity_check`` (default ON, Phase 4.10 Task A) — advisory
      cyclomatic-complexity gate.
    - ``secret_detect`` (default ON, Phase 4.10 Task B) — PreToolUse
      regex scan for AWS / GitHub / OpenAI keys, PEM, JWT, passwords.
    - ``sql_injection_guard`` (default ON, Phase 4.10 Task B) —
      PreToolUse regex scan for string-built SQL queries.
    - ``unsafe_import_block`` (default ON, Phase 4.10 Task B) —
      PreToolUse regex scan for dangerous imports in *.py content.

The Phase 4.10 security hooks (Task B) use ``user.builtin.*`` hook_ids
(loaded from ``.harness/hooks/*.json`` via the user-spec mechanism, NOT
the process-level ``builtin.*`` registry path). They are still exported
from this subpackage for direct testing.

Each is a public async function ``hook(context) -> HookDecision``.

Test code may import individual hooks from this subpackage
(``from harness.hooks.builtin import log_hook``) — production code
goes through the registry (no direct import).
"""
from __future__ import annotations

from harness.hooks.builtin.autosave import autosave_hook
from harness.hooks.builtin.block_dangerous import block_dangerous_hook
from harness.hooks.builtin.complexity_check import complexity_check_hook
from harness.hooks.builtin.confirm_dangerous import confirm_dangerous_hook
from harness.hooks.builtin.inject_context import inject_context_hook
from harness.hooks.builtin.license_check import license_check_hook
from harness.hooks.builtin.log import log_hook
from harness.hooks.builtin.notify_terminal import notify_terminal_hook
from harness.hooks.builtin.secret_detect import secret_detect_hook
from harness.hooks.builtin.sql_injection_guard import sql_injection_guard_hook
from harness.hooks.builtin.unsafe_import_block import unsafe_import_block_hook
from harness.hooks.builtin.validate import validate_hook

BUILTIN_HOOKS: dict[str, object] = {
    "log": log_hook,
    "validate": validate_hook,
    "block_dangerous": block_dangerous_hook,
    "inject_context": inject_context_hook,
    "autosave": autosave_hook,
    "confirm_dangerous": confirm_dangerous_hook,
    "notify_terminal": notify_terminal_hook,
    # Phase 4.10 Task A: advisory / policy hooks.
    "license_check": license_check_hook,
    "complexity_check": complexity_check_hook,
    # Phase 4.10 Task B: 3 security hooks (fail-closed).
    "secret_detect": secret_detect_hook,
    "sql_injection_guard": sql_injection_guard_hook,
    "unsafe_import_block": unsafe_import_block_hook,
}

__all__ = [
    "log_hook",
    "validate_hook",
    "block_dangerous_hook",
    "inject_context_hook",
    "autosave_hook",
    "confirm_dangerous_hook",
    "notify_terminal_hook",
    "license_check_hook",
    "complexity_check_hook",
    "secret_detect_hook",
    "sql_injection_guard_hook",
    "unsafe_import_block_hook",
    "BUILTIN_HOOKS",
]
