"""Phase 3 v1.5.0 Step 2: tests for harness.privacy.zone_config.parse_zones.

Covers:
- Empty patterns_str → built-in defaults (6 entries)
- Comma-separated patterns_str → user overrides
- per_action_str parsing (pattern=action pairs)
- Invalid actions raise ValueError
- Invalid format (missing =) raises ValueError
- Deduplication of patterns
- Invalid default_action raises ValueError
"""
from __future__ import annotations

import pytest

from harness.privacy.zone_config import (
    DEFAULT_ZONE_PATTERNS,
    ZoneRule,
    parse_zones,
)


class TestParseZones:
    """parse_zones() correctness."""

    def test_empty_patterns_uses_defaults(self) -> None:
        """Empty patterns_str → DEFAULT_ZONE_PATTERNS."""
        rules = parse_zones("", "", "block")
        assert len(rules) == len(DEFAULT_ZONE_PATTERNS)
        assert [r.pattern for r in rules] == list(DEFAULT_ZONE_PATTERNS)
        # All use default_action "block".
        assert all(r.action == "block" for r in rules)

    def test_whitespace_only_patterns_uses_defaults(self) -> None:
        """Whitespace-only patterns_str → still treated as empty."""
        rules = parse_zones("   ", "  ", "skip")
        assert len(rules) == len(DEFAULT_ZONE_PATTERNS)
        # All use default_action "skip" (whitespace per_action_str).
        assert all(r.action == "skip" for r in rules)

    def test_custom_patterns_split(self) -> None:
        """Comma-separated patterns_str → list of ZoneRule."""
        rules = parse_zones("*.key,*.pem,*.p12", "", "block")
        assert len(rules) == 3
        assert [r.pattern for r in rules] == ["*.key", "*.pem", "*.p12"]
        assert all(r.action == "block" for r in rules)

    def test_per_action_override(self) -> None:
        """per_action_str pattern=action pairs override default."""
        rules = parse_zones(
            "*.key,*.pem,*.p12",
            "*.key=skip,*.pem=redact",
            "block",
        )
        assert len(rules) == 3
        assert rules[0] == ZoneRule(pattern="*.key", action="skip")
        assert rules[1] == ZoneRule(pattern="*.pem", action="redact")
        assert rules[2] == ZoneRule(pattern="*.p12", action="block")  # default

    def test_per_action_with_whitespace(self) -> None:
        """Whitespace in per_action_str is stripped."""
        rules = parse_zones("a,b", "  a = redact , b = skip ", "block")
        assert rules[0].action == "redact"
        assert rules[1].action == "skip"

    def test_per_action_invalid_format_raises(self) -> None:
        """per_action_str token without ``=`` raises ValueError."""
        with pytest.raises(ValueError, match="missing '='"):
            parse_zones("a,b", "redact", "block")  # no '=' in "redact"

    def test_per_action_invalid_action_raises(self) -> None:
        """Unknown action in per_action_str raises ValueError."""
        with pytest.raises(ValueError, match="invalid action 'delete'"):
            parse_zones("a", "a=delete", "block")

    def test_default_action_invalid_raises(self) -> None:
        """default_action not in {block,redact,skip} raises ValueError."""
        with pytest.raises(ValueError, match="invalid default_action 'purge'"):
            parse_zones("a", "", "purge")

    def test_dedup_preserves_first_occurrence(self) -> None:
        """Duplicate patterns collapse to one ZoneRule (first wins)."""
        rules = parse_zones("a,b,a,c,b", "", "block")
        assert [r.pattern for r in rules] == ["a", "b", "c"]
        assert len(rules) == 3

    def test_empty_patterns_in_csv_are_skipped(self) -> None:
        """Empty tokens from double-comma are skipped, not errored."""
        rules = parse_zones("a,,b,", "", "block")
        assert [r.pattern for r in rules] == ["a", "b"]


class TestZoneRuleDataclass:
    """ZoneRule is frozen dataclass."""

    def test_zone_rule_is_frozen(self) -> None:
        rule = ZoneRule(pattern="*.env", action="block")
        with pytest.raises((AttributeError, TypeError)):
            rule.pattern = "*.key"  # type: ignore[misc]

    def test_zone_rule_equality(self) -> None:
        """Two ZoneRule with same fields are equal (frozen dataclass)."""
        a = ZoneRule(pattern="*.env", action="block")
        b = ZoneRule(pattern="*.env", action="block")
        assert a == b
