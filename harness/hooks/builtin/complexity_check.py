"""Phase 4.10 Task A: complexity_check builtin hook (PostToolUse).

AST-based cyclomatic-complexity estimate for Python files written or
edited via ``write_file`` / ``edit_file``. Emits a WARNING log when
the complexity of any top-level function exceeds the configured
threshold. Never blocks — purely advisory.

The complexity metric counts decision points:
    if / elif / for / while / except / boolean-and / boolean-or

Threshold:
    ``Settings.hooks_complexity_threshold`` (int, default 10). Tests
    can override via the module-level ``_THRESHOLD_OVERRIDE`` slot.

Trust boundary: stdlib (``ast``) + ``harness.hooks.context`` only.
The lazy ``harness.config`` import is permitted by the trust-boundary
test's ALLOWED_PREFIXES.
"""
from __future__ import annotations

import ast
import logging
from typing import Any

from harness.hooks.context import HookContext, HookDecision

logger = logging.getLogger("harness.hooks.builtin.complexity_check")

_DEFAULT_THRESHOLD: int = 10

# Test override slot. When non-None, takes precedence over Settings.
_THRESHOLD_OVERRIDE: int | None = None

# Tools whose arguments carry a file's text content.
_TEXT_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})


def _resolve_threshold() -> int:
    """Return the active complexity threshold (override > Settings > default)."""
    if _THRESHOLD_OVERRIDE is not None:
        return _THRESHOLD_OVERRIDE
    try:
        from harness.config import get_settings

        val = get_settings().hooks_complexity_threshold
        if isinstance(val, int) and val > 0:
            return val
    except Exception:  # noqa: BLE001 — Settings may be unavailable
        pass
    return _DEFAULT_THRESHOLD


# Node types that each add one decision point to cyclomatic complexity.
_DECISION_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
)

# Boolean operators (``and`` / ``or``) each add n-1 decision points
# per boolean expression (a and b and c → 2 extra branches).


def _complexity_of(node: ast.AST) -> int:
    """Estimate cyclomatic complexity of ``node`` (a function body).

    Base complexity is 1 (the function itself); each decision node and
    each boolean sub-expression adds 1.
    """
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, _DECISION_NODES):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            # ``a and b and c`` has len(values)=3 → 2 extra branches.
            complexity += max(0, len(child.values) - 1)
    return complexity


def _functions(tree: ast.AST) -> list[tuple[str, int]]:
    """Return [(func_name, complexity), ...] for every FunctionDef in tree."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, _complexity_of(node)))
    return out


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull candidate file text from a PostToolUse payload (or '')."""
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        for key in ("content", "new_string", "text"):
            val = arguments.get(key)
            if isinstance(val, str) and val:
                return val
    if isinstance(arguments, str) and arguments:
        return arguments
    return ""


async def complexity_check_hook(context: HookContext) -> HookDecision:
    """Warn (not block) when written Python code exceeds the complexity threshold."""
    if context.event != "PostToolUse":
        return HookDecision(decision="allow", hook_id="builtin.complexity_check")
    payload = context.payload or {}
    tool_name = payload.get("tool_name", "")
    if tool_name not in _TEXT_TOOLS:
        return HookDecision(decision="allow", hook_id="builtin.complexity_check")
    text = _extract_text(payload)
    if not text:
        return HookDecision(decision="allow", hook_id="builtin.complexity_check")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # Malformed Python — advisory hook, never block on a parse error.
        logger.debug(
            "complexity_check: skipped unparsable content (%s)", exc.msg
        )
        return HookDecision(decision="allow", hook_id="builtin.complexity_check")
    threshold = _resolve_threshold()
    offenders = [(name, c) for name, c in _functions(tree) if c > threshold]
    for name, c in offenders:
        logger.warning(
            "complexity_check: %s has complexity %d (threshold %d)",
            name,
            c,
            threshold,
        )
    return HookDecision(decision="allow", hook_id="builtin.complexity_check")


__all__ = ["complexity_check_hook"]
