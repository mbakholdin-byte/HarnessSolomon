"""Phase 3 v1.5.0 Step 2: tests for harness.privacy.zone_filter.PrivacyZoneFilter.

Covers:
- allow on no match (4 cases)
- block / redact / skip on match (3 cases)
- disabled filter = allow (2 cases)
- audit event emitted on hit (2 cases)
- first-match-wins semantics (1 case)
- should_exclude convenience (2 cases)
- empty rules list (1 case)
"""
from __future__ import annotations

from typing import Any

import pytest

from harness.privacy.zone_config import ZoneRule, parse_zones
from harness.privacy.zone_filter import PrivacyZoneFilter


class FakeAudit:
    """Test double for ScratchAudit.record() — captures (event, payload) tuples."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._raises = raises

    def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
        if self._raises:
            raise RuntimeError("audit backend unavailable")
        if payload is None:
            self.calls.append((event, {}))
        else:
            self.calls.append((event, payload))


class TestZoneFilter:
    """PrivacyZoneFilter.check() and .should_exclude()."""

    def test_allow_on_no_match(self) -> None:
        """No rule matches → ('allow', None)."""
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules)
        assert f.check("src/main.py") == ("allow", None)
        assert f.check("docs/index.md") == ("allow", None)

    def test_block_on_match(self) -> None:
        """Match + action=block → ('block', 'pattern')."""
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules)
        assert f.check("private/.env") == ("block", "private/**")
        assert f.check("private/secrets/key.pem") == ("block", "private/**")

    def test_redact_on_match(self) -> None:
        """Match + action=redact → ('redact', 'pattern')."""
        rules = [ZoneRule(pattern="secrets/**", action="redact")]
        f = PrivacyZoneFilter(rules)
        assert f.check("secrets/api_key") == ("redact", "secrets/**")

    def test_skip_on_match(self) -> None:
        """Match + action=skip → ('skip', 'pattern')."""
        rules = [ZoneRule(pattern=".env", action="skip")]
        f = PrivacyZoneFilter(rules)
        assert f.check(".env") == ("skip", ".env")
        assert f.check("a/b/.env") == ("skip", ".env")  # basename fallback

    def test_first_match_wins(self) -> None:
        """When multiple rules match, the FIRST one wins.

        ``*.env`` (basename fallback) is broader than ``**/*.env``
        (anchored at start with `/`), so we put ``*.env`` first to
        demonstrate first-match-wins.
        """
        rules = [
            ZoneRule(pattern="*.env", action="block"),
            ZoneRule(pattern="**/*.env", action="redact"),
        ]
        f = PrivacyZoneFilter(rules)
        # First rule matches via basename fallback.
        assert f.check("private/.env") == ("block", "*.env")
        # Rule 2 never evaluated.

    def test_disabled_filter_allows_all(self) -> None:
        """enabled=False short-circuits to ('allow', None) on every path."""
        rules = [ZoneRule(pattern="**", action="block")]
        f = PrivacyZoneFilter(rules, enabled=False)
        assert f.check("private/.env") == ("allow", None)
        assert f.check("src/main.py") == ("allow", None)
        # Audit is also skipped (no event).
        audit = FakeAudit()
        f2 = PrivacyZoneFilter(rules, enabled=False, audit=audit)
        f2.check("private/.env")
        assert audit.calls == []

    def test_empty_rules_list(self) -> None:
        """No rules → everything is allowed."""
        f = PrivacyZoneFilter([])
        assert f.check("private/.env") == ("allow", None)
        assert f.check("any/path") == ("allow", None)


class TestShouldExclude:
    """should_exclude() convenience method."""

    def test_block_action_excluded(self) -> None:
        """block → should_exclude=True."""
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules)
        assert f.should_exclude("private/.env") is True

    def test_redact_action_not_excluded(self) -> None:
        """redact → should_exclude=False (caller wants the placeholder)."""
        rules = [ZoneRule(pattern="*.env", action="redact")]
        f = PrivacyZoneFilter(rules)
        assert f.should_exclude("private/.env") is False

    def test_skip_action_not_excluded(self) -> None:
        """skip → should_exclude=False (caller wants the empty result)."""
        rules = [ZoneRule(pattern=".env", action="skip")]
        f = PrivacyZoneFilter(rules)
        assert f.should_exclude(".env") is False


class TestAuditIntegration:
    """PrivacyZoneFilter audit emit behaviour."""

    def test_audit_emitted_on_block(self) -> None:
        """Match + action=block → audit 'privacy_zone_blocked'."""
        audit = FakeAudit()
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules, audit=audit)
        f.check("private/.env")
        assert len(audit.calls) == 1
        event, payload = audit.calls[0]
        assert event == "privacy_zone_blocked"
        assert payload == {
            "action": "block",
            "path": "private/.env",
            "pattern": "private/**",
        }

    def test_audit_emitted_per_action(self) -> None:
        """Each non-allow action emits its own audit event name.

        Paths use a directory prefix (e.g. ``x/a.block``) so the
        ``**/*.X`` patterns (regex ``.*/.*\\.X``) match via re.match.
        """
        audit = FakeAudit()
        rules = [
            ZoneRule(pattern="**/*.block", action="block"),
            ZoneRule(pattern="**/*.redact", action="redact"),
            ZoneRule(pattern="**/*.skip", action="skip"),
        ]
        f = PrivacyZoneFilter(rules, audit=audit)
        f.check("x/a.block")
        f.check("x/a.redact")
        f.check("x/a.skip")
        events = [c[0] for c in audit.calls]
        assert events == [
            "privacy_zone_blocked",
            "privacy_zone_redacted",
            "privacy_zone_skipped",
        ]

    def test_audit_not_emitted_on_allow(self) -> None:
        """No match → no audit (allow is silent)."""
        audit = FakeAudit()
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules, audit=audit)
        f.check("src/main.py")
        assert audit.calls == []

    def test_audit_none_is_noop(self) -> None:
        """audit=None → no error, no emit (default)."""
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules)  # audit=None default
        f.check("private/.env")  # no exception

    def test_audit_failure_is_failsafe(self) -> None:
        """Audit backend raises → filter does NOT raise (fail-open)."""
        audit = FakeAudit(raises=True)
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules, audit=audit)
        # Must not raise — fail-open at audit boundary.
        assert f.check("private/.env") == ("block", "private/**")


class TestPropertiesAndHelpers:
    """Property accessors and introspection."""

    def test_enabled_property_reflects_ctor(self) -> None:
        """enabled property mirrors the constructor argument."""
        assert PrivacyZoneFilter([], enabled=True).enabled is True
        assert PrivacyZoneFilter([], enabled=False).enabled is False

    def test_rules_property_returns_copy(self) -> None:
        """rules property returns a defensive copy (caller can't mutate)."""
        original = [ZoneRule(pattern="*.env", action="block")]
        f = PrivacyZoneFilter(original)
        view = f.rules
        view.append(ZoneRule(pattern="*.key", action="redact"))  # type: ignore[arg-type]
        # Internal rules unchanged.
        assert len(f.rules) == 1

    def test_integration_with_parse_zones_defaults(self) -> None:
        """End-to-end: parse_zones defaults + filter → blocks sensitive files.

        Default patterns:
            ('private/**', '*.env', '.env/**', 'secrets/**',
             '_credentials/**', '.ssh/**')
        First match wins, so 'private/.env' matches 'private/**' first
        (block). For 'config/.env' the basename fallback for '*.env'
        matches (block). Plain source files are allowed.
        """
        rules = parse_zones("", "", "block")
        f = PrivacyZoneFilter(rules)
        # private/** matches anything under private/ (recursive `**`).
        assert f.check("private/.env")[0] == "block"
        assert f.check("private/secrets/key.pem")[0] == "block"
        # *.env matches any .env file (basename fallback).
        assert f.check("config/.env")[0] == "block"
        # secrets/** matches anything under secrets/.
        assert f.check("secrets/api_key")[0] == "block"
        # .ssh/** matches SSH keys.
        assert f.check("home/user/.ssh/id_rsa")[0] == "block"
        # Plain source files are allowed.
        assert f.check("src/main.py") == ("allow", None)
        assert f.check("docs/index.md") == ("allow", None)
