"""Phase 4.10 Task A: license_check builtin hook (PreToolUse).

Scans the proposed file content for ``import`` statements that pull
in packages known to be distributed under forbidden licenses
(GPL-3.0, AGPL-3.0, SSPL, Commons Clause). Blocks the write when a
match is found; otherwise allows.

Configuration:
    The forbidden set is read from ``Settings.hooks_license_check_forbidden``
    (a comma-separated string). If the setting is absent or empty, a
    conservative default set is used. The hook is intentionally
    configurable so operators can relax or tighten the policy per project.

Trust boundary: stdlib (``re``) + ``harness.hooks.context`` only.
No ``harness.agents`` / ``harness.server`` imports. The lazy
``harness.config`` import is allowed by the trust boundary test
(see ``tests/test_hooks_trust_boundary.py`` ALLOWED_PREFIXES).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from harness.hooks.context import HookContext, HookDecision

logger = logging.getLogger("harness.hooks.builtin.license_check")

# Conservative defaults. Operators can extend/override via
# ``Settings.hooks_license_check_forbidden`` (comma-separated list of
# package-name fragments; each is matched as a case-insensitive substring
# inside an ``import`` / ``from ... import`` statement).
_DEFAULT_FORBIDDEN: tuple[str, ...] = (
    # GPL-3.0 family
    "gpl3",
    "gpl-3",
    # AGPL-3.0 family
    "agpl3",
    "agpl-3",
    # Server Side Public License
    "sspl",
    # Commons Clause (licensed-but-not-open-source)
    "commons-clause",
    "commons_clause",
)

# Module-level override slot for tests. When non-None, takes precedence
# over both the default and the Settings field. Tests can ``monkeypatch``
# this attribute to inject a deterministic forbidden set without spinning
# up a real Settings instance.
_FORBIDDEN_OVERRIDE: tuple[str, ...] | None = None


def _resolve_forbidden() -> tuple[str, ...]:
    """Return the active forbidden-fragment set.

    Priority: test override > Settings field > module default.
    """
    if _FORBIDDEN_OVERRIDE is not None:
        return _FORBIDDEN_OVERRIDE
    try:
        from harness.config import get_settings

        raw = (get_settings().hooks_license_check_forbidden or "").strip()
    except Exception:  # noqa: BLE001 — Settings may be unavailable in tests
        return _DEFAULT_FORBIDDEN
    if not raw:
        return _DEFAULT_FORBIDDEN
    parts = tuple(
        p.strip().lower()
        for p in raw.split(",")
        if p.strip()
    )
    return parts or _DEFAULT_FORBIDDEN


def _build_pattern(forbidden: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a single alternation regex matching import statements.

    The pattern matches ``import X`` and ``from X import ...`` lines
    where X contains one of the forbidden fragments. Case-insensitive.
    """
    if not forbidden:
        # Match nothing.
        return re.compile(r"(?!x)x")  # unmatchable
    alternation = "|".join(re.escape(f) for f in forbidden)
    # We deliberately keep this loose: a fragment like "sspl" should
    # catch ``import sspl_lic`` and ``from sspl_lic import thing`` alike.
    return re.compile(
        rf"^\s*(?:import\s+\S*|(?:from)\s+\S*\s+import\s+)\S*(?:{alternation})\S*",
        re.IGNORECASE | re.MULTILINE,
    )


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull the candidate file text out of a PreToolUse payload.

    Looks for ``content`` / ``new_string`` (write_file / edit_file args)
    and falls back to a stringified view of ``arguments``. Returns ''
    when no usable text is present.
    """
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        for key in ("content", "new_string", "text"):
            val = arguments.get(key)
            if isinstance(val, str) and val:
                return val
    # If arguments is itself a string (rare but supported by the schema),
    # scan it directly.
    if isinstance(arguments, str) and arguments:
        return arguments
    return ""


async def license_check_hook(context: HookContext) -> HookDecision:
    """Block PreToolUse if the proposed content imports a forbidden-license package."""
    if context.event != "PreToolUse":
        return HookDecision(decision="allow", hook_id="builtin.license_check")
    payload = context.payload or {}
    text = _extract_text(payload)
    if not text:
        # No text to scan (e.g. a read-only tool) — allow.
        return HookDecision(decision="allow", hook_id="builtin.license_check")
    forbidden = _resolve_forbidden()
    pattern = _build_pattern(forbidden)
    match = pattern.search(text)
    if match is None:
        return HookDecision(decision="allow", hook_id="builtin.license_check")
    matched_line = match.group(0).strip()
    reason = (
        f"forbidden-license import detected: {matched_line!r} "
        f"(matches fragments: {', '.join(forbidden)})"
    )
    logger.warning("license_check: %s", reason)
    return HookDecision(
        decision="block",
        hook_id="builtin.license_check",
        output={"reason": reason[:500]},
    )


__all__ = ["license_check_hook"]
