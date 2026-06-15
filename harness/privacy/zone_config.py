"""Phase 3 v1.5.0 Step 2: Privacy zone configuration parser.

Converts Settings strings (``privacy_zone_patterns``, ``privacy_zone_per_action``)
into structured :class:`ZoneRule` list.

Parser behaviour:
- Empty ``patterns_str`` → built-in defaults (6 entries from
  ``_DEFAULT_PATTERNS``).
- Empty ``per_action_str`` → all patterns use ``default_action``.
- ``per_action_str`` format: ``"pattern1=action1,pattern2=action2"``.
  Actions must be in ``_VALID_ACTIONS`` (otherwise raises ``ValueError``).

Pattern syntax: same as :mod:`harness.privacy.path_match` glob
(``*``, ``**``, ``?``, anchored ``/``, trailing ``/``, negation ``!``).
See :func:`harness.privacy.path_match.match_glob` for full grammar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["ZoneRule", "parse_zones", "DEFAULT_ZONE_PATTERNS"]

# Literal type re-exported for Pydantic v2 settings validator.
ZoneAction = Literal["block", "redact", "skip"]
_VALID_ACTIONS: Final[frozenset[str]] = frozenset({"block", "redact", "skip"})

# Default privacy patterns applied when ``privacy_zone_patterns`` is empty.
# Chosen to cover the most common sensitive file conventions:
# - private/** : per-project private dir (common in monorepos)
# - *.env / .env/** : dotenv files at any depth (basename + nested)
# - secrets/** : explicit secrets directory
# - _credentials/** : underscore-prefixed convention (Django, Rails)
# - .ssh/** / **/.ssh/** : SSH keys at repo root AND nested
#   (fnmatch `**` is anchored; we need both forms for "anywhere")
DEFAULT_ZONE_PATTERNS: Final[tuple[str, ...]] = (
    "private/**",
    "*.env",
    ".env/**",
    "secrets/**",
    "_credentials/**",
    ".ssh/**",
    "**/.ssh/**",
)


@dataclass(frozen=True)
class ZoneRule:
    """One privacy zone rule: pattern + action.

    Attributes:
        pattern: Glob pattern (see :mod:`harness.privacy.path_match`).
        action:  What to do on match — ``block`` (raise / return error),
                 ``redact`` (replace content with placeholder), or
                 ``skip`` (silent skip, return empty result).
    """

    pattern: str
    action: ZoneAction


def parse_zones(
    patterns_str: str,
    per_action_str: str,
    default_action: str,
) -> list[ZoneRule]:
    """Parse Settings strings into a list of :class:`ZoneRule`.

    Args:
        patterns_str:   Comma-separated list of glob patterns. Empty
                        string → use :data:`DEFAULT_ZONE_PATTERNS`.
        per_action_str: Comma-separated ``pattern=action`` overrides.
                        Format: ``"private/**=redact,secrets/*=block"``.
                        Empty string → all patterns use ``default_action``.
        default_action: Fallback action for patterns without explicit
                        override. Must be one of ``block``, ``redact``,
                        ``skip``.

    Returns:
        Ordered list of :class:`ZoneRule` ready for
        :class:`harness.privacy.zone_filter.PrivacyZoneFilter`.

    Raises:
        ValueError: If ``default_action`` or any per-action override
                    is not in ``_VALID_ACTIONS``.

    Examples:
        >>> parse_zones("", "", "block")
        [ZoneRule(pattern='private/**', action='block'), ...]

        >>> parse_zones("*.key", "*.key=skip", "block")
        [ZoneRule(pattern='*.key', action='skip')]

        >>> parse_zones("a,b,c", "a=redact", "block")
        [ZoneRule(pattern='a', action='redact'),
         ZoneRule(pattern='b', action='block'),
         ZoneRule(pattern='c', action='block')]
    """
    if default_action not in _VALID_ACTIONS:
        raise ValueError(
            f"invalid default_action {default_action!r}; "
            f"must be one of {sorted(_VALID_ACTIONS)}"
        )

    # Parse per-action overrides first (validate early).
    overrides: dict[str, str] = {}
    if per_action_str.strip():
        for token in per_action_str.split(","):
            token = token.strip()
            if not token:
                continue
            if "=" not in token:
                raise ValueError(
                    f"per_action override {token!r} missing '='; "
                    f"expected format 'pattern=action'"
                )
            pattern, _, action = token.partition("=")
            pattern = pattern.strip()
            action = action.strip()
            if action not in _VALID_ACTIONS:
                raise ValueError(
                    f"per_action override for {pattern!r} has invalid "
                    f"action {action!r}; must be one of {sorted(_VALID_ACTIONS)}"
                )
            overrides[pattern] = action

    # Resolve patterns: empty → defaults.
    if patterns_str.strip():
        patterns = tuple(
            p.strip() for p in patterns_str.split(",") if p.strip()
        )
    else:
        patterns = DEFAULT_ZONE_PATTERNS

    # Dedupe patterns preserving first-occurrence order.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            deduped.append(p)

    return [
        ZoneRule(pattern=p, action=overrides.get(p, default_action))  # type: ignore[arg-type]
        for p in deduped
    ]
