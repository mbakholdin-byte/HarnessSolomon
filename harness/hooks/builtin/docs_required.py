"""Phase 4.10 Task C: Builtin docs_required hook (PostToolUse).

Inspects ``*.py`` files after a write/edit and logs a warning for
every public function / class / async function that lacks a
docstring. Informational only — never blocks.

Behaviour:
    1. Ignore any event other than ``PostToolUse`` (allow).
    2. Resolve the target file path from the payload. If absent or
       not a ``.py`` file, allow (nothing to inspect).
    3. Read and ``ast.parse`` the file. On any IO / SyntaxError,
       allow (fail-open — we cannot inspect what we cannot parse).
    4. Walk top-level and nested ``FunctionDef`` /
       ``AsyncFunctionDef`` / ``ClassDef`` nodes whose ``name`` does
       NOT start with ``_``. For each, check ``ast.get_docstring``.
    5. Log ``WARNING`` "Missing docstring on public function: <name>"
       for every offender.
    6. Return ``allow`` regardless (this hook is advisory).

Trust boundary: stdlib (``ast``, ``logging``, ``pathlib``) +
``harness.hooks.context`` only. No ``harness.agents`` /
``harness.server`` imports.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.docs_required")


# AST node types that declare a public symbol whose docstring we
# care about. ``AsyncFunctionDef`` is a subclass of ``FunctionDef``
# in CPython's AST, but we list both explicitly for clarity and
# forward compatibility.
_PUBLIC_DECL_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


def _extract_path(payload: dict[str, Any]) -> str:
    """Return the file path targeted by a write/edit tool payload.

    Supports ``arguments.path`` (write_file / edit_file) and a
    top-level ``path`` field. Returns ``""`` if absent.
    """
    arguments = payload.get("arguments", {})
    if isinstance(arguments, dict):
        p = arguments.get("path")
        if isinstance(p, str):
            return p
    p = payload.get("path")
    return p if isinstance(p, str) else ""


def _find_missing_docstrings(source: str) -> list[str]:
    """Return names of public decls missing a docstring.

    A decl is "public" if its name does not start with ``_``.
    Walks the entire tree (top-level + nested), because a nested
    public class inside a private module wrapper still represents
    a public API surface from the reader's perspective.
    """
    tree = ast.parse(source)
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, _PUBLIC_DECL_NODES):
            continue
        if node.name.startswith("_"):
            continue
        if ast.get_docstring(node) is None:
            missing.append(node.name)
    return missing


async def docs_required_hook(context: HookContext) -> HookDecision:
    """Log warnings for public decls missing docstrings (advisory)."""
    hook_id = "user.builtin.docs_required"

    if context.event != "PostToolUse":
        return HookDecision(decision="allow", hook_id=hook_id)

    path_str = _extract_path(context.payload)
    if not path_str or not path_str.endswith(".py"):
        return HookDecision(decision="allow", hook_id=hook_id)

    path = Path(path_str)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug(
            "docs_required: cannot read %s (%s); failing open",
            path_str,
            exc,
        )
        return HookDecision(decision="allow", hook_id=hook_id)

    try:
        missing = _find_missing_docstrings(source)
    except SyntaxError as exc:
        logger.debug(
            "docs_required: %s has SyntaxError (%s); failing open",
            path_str,
            exc,
        )
        return HookDecision(decision="allow", hook_id=hook_id)

    for name in missing:
        logger.warning(
            "Missing docstring on public function: %s (in %s)",
            name,
            path_str,
        )

    return HookDecision(
        decision="allow",
        hook_id=hook_id,
        output={"missing_docstrings": missing, "path": path_str},
    )


__all__ = ["docs_required_hook"]
