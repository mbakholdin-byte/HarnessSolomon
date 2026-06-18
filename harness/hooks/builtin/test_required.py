"""Phase 4.10 Task C: Builtin test_required hook (PreToolUse).

Gates ``git commit`` invocations: if the staged tree contains any
``*.py`` files, the commit command must also invoke ``pytest``
(either directly in the command or as a chained pre-commit step).
Otherwise the hook returns ``block`` with a human-readable reason.

Behaviour:
    1. Ignore any event other than ``PreToolUse`` (allow).
    2. Ignore any tool whose ``arguments.command`` is not a
       ``git commit`` invocation (allow).
    3. Run ``git diff --name-only --cached`` (subprocess, read-only).
    4. If no ``.py`` files are staged, allow (nothing to gate).
    5. If ``pytest`` appears anywhere in the command string, allow.
    6. Otherwise block with reason ``"tests required: detected N .py
       changes, run pytest first"``.

Trust boundary: stdlib (``re``, ``subprocess``, ``logging``) +
``harness.hooks.context`` only. No ``harness.agents`` /
``harness.server`` imports.
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.test_required")


# Match the start of a ``git commit`` command. Allows leading
# whitespace and an optional shell prefix (``sh -c``). The token
# ``git`` must be followed by ``commit`` (possibly with flags
# in between, e.g. ``git -C /repo commit``).
_GIT_COMMIT_RE = re.compile(
    r"(?:^|\s|;|&&|\|\|)"   # boundary: start, whitespace, or separator
    r"git\s+[^|;&]*\bcommit\b",
    re.IGNORECASE,
)


def _extract_command(payload: dict[str, Any]) -> str:
    """Return the shell command string from a PreToolUse payload.

    Supports both ``arguments.command`` (bash tool) and a top-level
    ``command`` field. Returns ``""`` if absent.
    """
    arguments = payload.get("arguments", {})
    if isinstance(arguments, dict):
        cmd = arguments.get("command")
        if isinstance(cmd, str):
            return cmd
    cmd = payload.get("command")
    return cmd if isinstance(cmd, str) else ""


def _is_git_commit(command: str) -> bool:
    """True if the command invokes ``git commit``."""
    return bool(_GIT_COMMIT_RE.search(command))


def _staged_python_files() -> list[str]:
    """Return the list of staged ``*.py`` paths.

    Uses ``git diff --name-only --cached``. On any subprocess error
    (not a git repo, git missing, etc.) returns an empty list —
    fail-open: we cannot prove there are .py changes, so we do not
    block.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.debug(
            "test_required: git diff failed (%s); failing open", exc
        )
        return []
    if proc.returncode != 0:
        logger.debug(
            "test_required: git diff exit=%d stderr=%r; failing open",
            proc.returncode,
            proc.stderr.strip(),
        )
        return []
    files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return [f for f in files if f.endswith(".py")]


async def test_required_hook(context: HookContext) -> HookDecision:
    """Block ``git commit`` on staged ``*.py`` changes unless ``pytest`` runs."""
    hook_id = "user.builtin.test_required"

    if context.event != "PreToolUse":
        return HookDecision(decision="allow", hook_id=hook_id)

    command = _extract_command(context.payload)
    if not command or not _is_git_commit(command):
        return HookDecision(decision="allow", hook_id=hook_id)

    py_files = _staged_python_files()
    if not py_files:
        # Nothing to gate.
        return HookDecision(decision="allow", hook_id=hook_id)

    if "pytest" in command:
        # The user explicitly runs pytest as part of the commit flow.
        return HookDecision(decision="allow", hook_id=hook_id)

    reason = (
        f"tests required: detected {len(py_files)} .py change(s), "
        f"run pytest first"
    )
    logger.warning("test_required: %s (files=%s)", reason, py_files[:3])
    return HookDecision(
        decision="block",
        hook_id=hook_id,
        output={"reason": reason, "staged_py_files": py_files},
    )


__all__ = ["test_required_hook"]
