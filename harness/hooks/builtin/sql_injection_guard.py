"""Phase 4.10 Task B: Builtin SqlInjectionGuardHook — block string-built SQL.

PreToolUse defence-in-depth: scans a ``bash`` / ``write_file`` /
``edit_file`` tool's flattened arguments for patterns that indicate a
SQL query was assembled via string interpolation rather than
parameterisation. Catches the four classic Python anti-patterns:

    * f-strings:       ``f"SELECT ... FROM ... {var}"``
    * concatenation:  ``"SELECT ..." + var``
    * %-formatting:   ``"...%s..." % var``
    * ``.format()``:  ``"...".format(var)``

Also catches ``DELETE FROM ... WHERE ...`` built by concatenation
(a common destructive-query smell).

The regexes require both a SQL keyword (``SELECT`` / ``DELETE``) AND an
interpolation marker. Parametrised calls like
``cursor.execute("SELECT * FROM users WHERE id=?", (id,))`` do NOT
match because there is no interpolation marker adjacent to the SQL
string literal.

Trust boundary: stdlib + ``re`` + ``logging`` only.
"""
from __future__ import annotations

import logging
import re

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.sql_injection_guard")


# Order matters: the first match wins. Each pair is
# (compiled_regex, human_label). Patterns are case-sensitive on
# ``SELECT`` / ``FROM`` / ``DELETE`` to match Python source literally
# (the agent is generating Python code, not natural language). The
# regex shapes are taken verbatim from the Phase 4.10 handoff so
# behaviour is reproducible across implementations.
_SQL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # f-string SQL (double-quoted): f"SELECT ... FROM ... {var}"
    (
        re.compile(r'f"[^"]*SELECT[^"]*FROM[^"]*\{[^}]+\}'),
        "f-string interpolation in SQL query",
    ),
    # f-string SQL (single-quoted variant)
    (
        re.compile(r"f'[^']*SELECT[^']*FROM[^']*\{[^}]+\}"),
        "f-string interpolation in SQL query",
    ),
    # String concatenation SQL: "SELECT ..." + var
    (
        re.compile(r'"[^"]*SELECT[^"]*"\s*\+'),
        "string concatenation in SQL query",
    ),
    # %-formatting SQL: %...SELECT...% var,
    # (handoff literal: %[^%]*SELECT[^%]*%[^,]+,)
    (
        re.compile(r'%[^%]*SELECT[^%]*%[^,]+,'),
        "%-formatting in SQL query",
    ),
    # .format() SQL: .format(... SELECT ...)
    # (handoff literal: \.format\([^)]*SELECT)
    (
        re.compile(r"\.format\([^)]*SELECT"),
        ".format() call embedding SQL",
    ),
    # DELETE FROM via concatenation (destructive pattern)
    (
        re.compile(r'["\']DELETE FROM .* WHERE.*["\']\s*\+'),
        "string concatenation in DELETE query",
    ),
)


def _args_to_string(arguments: object) -> str:
    """Flatten tool arguments into a single string for regex scanning.

    Identical to the helper in ``block_dangerous`` / ``secret_detect`` —
    duplicated here to keep the trust boundary at stdlib-only (no
    cross-hook import path that could later pull in heavier deps).
    """
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        return " ".join(_args_to_string(v) for v in arguments.values())
    if isinstance(arguments, (list, tuple)):
        return " ".join(_args_to_string(v) for v in arguments)
    return str(arguments)


async def sql_injection_guard_hook(context: HookContext) -> HookDecision:
    """Block PreToolUse if arguments contain a string-built SQL query.

    Only PreToolUse events are inspected; everything else passes through.
    Empty / missing arguments also short-circuit to ``allow``.
    """
    if context.event != "PreToolUse":
        return HookDecision(
            decision="allow", hook_id="user.builtin.sql_injection_guard"
        )
    arguments = context.payload.get("arguments", {})
    target = _args_to_string(arguments)
    if not target:
        return HookDecision(
            decision="allow", hook_id="user.builtin.sql_injection_guard"
        )
    for pattern, label in _SQL_PATTERNS:
        if pattern.search(target):
            reason = f"SQL injection risk: {label}"
            logger.warning("SqlInjectionGuard: %s", reason)
            return HookDecision(
                decision="block",
                hook_id="user.builtin.sql_injection_guard",
                output={"reason": reason},
            )
    return HookDecision(
        decision="allow", hook_id="user.builtin.sql_injection_guard"
    )


__all__ = ["sql_injection_guard_hook"]
