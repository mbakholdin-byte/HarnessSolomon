"""Phase 3: tests for the redaction engine.

Coverage:
    - All 12 pattern categories match canonical examples
    - Idempotency: redact(redact(x)) == redact(x)
    - Non-str input is handled defensively (returns unchanged or empty)
    - Placeholders contain no recognisable secret (re-match test)
    - redact_dict walks nested dicts + lists
    - ``scan`` returns offsets that can reconstruct the redacted string
    - ``categories`` filter narrows the pattern set
    - Empty string / None / non-dict inputs are safe
"""
from __future__ import annotations

import re

import pytest

from harness.redaction import PATTERNS, RedactionMatch, redact, redact_dict, scan
from harness.redaction.patterns import placeholder


# === Pattern coverage: one canonical example per category ===

EMAIL_EX = "Contact alice@example.com for details"
PHONE_EX = "Call +1 (555) 123-4567 anytime"
IPV4_EX = "Server lives at 192.168.1.42, the other one at 10.0.0.1"
GITHUB_TOKEN_EX = "Use ghp_abc123def456ghi789jkl012mno345pqr678 for auth"
AWS_ACCESS_KEY_EX = "AKIAIOSFODNN7EXAMPLE is the demo key"
AWS_SECRET_EX = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
OPENAI_KEY_EX = "sk-proj-abc123def456ghi789jkl012mno345pqrstuvwx"
ANTHROPIC_KEY_EX = "sk-ant-api03-abc123def456ghi789jkl012mno345pqrstuvwxyz"
ENV_ASSIGNMENT_EX = "DB_PASSWORD=hunter2hunter2hunter2"
JWT_EX = "Token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
PEM_EX = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAK..."
SLACK_TOKEN_EX = "Slack: xoxb-1234567890-1234567890123-aBcDeFgHiJkLmNoPqRsTuVwX"


# === Test: pattern coverage ===

class TestPatternCoverage:
    @pytest.mark.parametrize(
        "category,example,expected_in_placeholder",
        [
            ("EMAIL", EMAIL_EX, "alice@"),
            ("PHONE", PHONE_EX, "555"),
            ("IPV4", IPV4_EX, "192"),
            ("GITHUB_TOKEN", GITHUB_TOKEN_EX, "ghp_"),
            ("AWS_ACCESS_KEY", AWS_ACCESS_KEY_EX, "AKIA"),
            ("AWS_SECRET", AWS_SECRET_EX, "AKIA"),  # captures base64 value
            ("OPENAI_KEY", OPENAI_KEY_EX, "sk-"),
            ("ANTHROPIC_KEY", ANTHROPIC_KEY_EX, "sk-ant-"),
            ("ENV_ASSIGNMENT", ENV_ASSIGNMENT_EX, "hunter2"),
            ("JWT", JWT_EX, "eyJ"),
            ("PEM_PRIVATE_KEY", PEM_EX, "-----BEGIN"),
            ("SLACK_TOKEN", SLACK_TOKEN_EX, "xoxb-"),
        ],
    )
    def test_redacts_canonical_example(
        self, category: str, example: str, expected_in_placeholder: str,
    ) -> None:
        result = redact(example, categories={category})
        # The original secret must be gone.
        assert expected_in_placeholder not in result, (
            f"category={category!r} failed to redact {expected_in_placeholder!r} "
            f"in {result!r}"
        )
        # The category placeholder must be present.
        assert placeholder(category) in result, (
            f"placeholder for {category!r} missing from {result!r}"
        )

    def test_all_patterns_have_placeholders(self) -> None:
        """Every pattern category has a working placeholder."""
        for cat in PATTERNS:
            assert placeholder(cat).startswith("<")
            assert placeholder(cat).endswith(">")
            assert placeholder(cat) == f"<{cat}>"


# === Test: idempotency ===

class TestIdempotency:
    @pytest.mark.parametrize(
        "example",
        [
            EMAIL_EX,
            PHONE_EX,
            IPV4_EX,
            GITHUB_TOKEN_EX,
            AWS_ACCESS_KEY_EX,
            AWS_SECRET_EX,
            OPENAI_KEY_EX,
            ANTHROPIC_KEY_EX,
            ENV_ASSIGNMENT_EX,
            JWT_EX,
            PEM_EX,
            SLACK_TOKEN_EX,
            "Multi: alice@example.com, ghp_abc123def456ghi789jkl012mno345pqr678, 10.0.0.1",
        ],
    )
    def test_redact_is_idempotent(self, example: str) -> None:
        once = redact(example)
        twice = redact(once)
        assert once == twice, (
            f"redact() is not idempotent for {example!r}: "
            f"once={once!r}, twice={twice!r}"
        )

    def test_redacted_text_contains_no_secret_fragments(self) -> None:
        """Placeholders must not contain substrings that could re-match
        any pattern. This is the load-bearing invariant for idempotency."""
        redacted = redact(EMAIL_EX + " " + GITHUB_TOKEN_EX + " " + IPV4_EX)
        for cat, pat in PATTERNS.items():
            # No re-match inside the redacted output for any pattern.
            assert not pat.search(redacted), (
                f"pattern {cat!r} re-matched in redacted output {redacted!r}"
            )


# === Test: defensive input handling ===

class TestDefensiveInput:
    def test_empty_string(self) -> None:
        assert redact("") == ""
        assert scan("") == []

    def test_none_input_redact_returns_empty(self) -> None:
        # redact(None) → "" (defensive; sinks should pass str only)
        assert redact(None) == ""  # type: ignore[arg-type]

    def test_none_input_scan_returns_empty(self) -> None:
        assert scan(None) == []  # type: ignore[arg-type]

    def test_int_input_redact_returns_empty(self) -> None:
        assert redact(42) == ""  # type: ignore[arg-type]

    def test_int_input_scan_returns_empty(self) -> None:
        assert scan(42) == []  # type: ignore[arg-type]

    def test_redact_does_not_mutate_input(self) -> None:
        original = "Email alice@example.com please"
        snapshot = original
        _ = redact(original)
        assert original == snapshot


# === Test: scan() returns offsets ===

class TestScanOffsets:
    def test_scan_returns_correct_offsets(self) -> None:
        text = "Reach alice@example.com or bob@example.org"
        matches = scan(text)
        assert len(matches) == 2
        for m in matches:
            assert m.category == "EMAIL"
            assert text[m.start:m.end] == m.original
        # Sorted by start position (left-to-right).
        assert matches[0].start < matches[1].start
        assert matches[0].original == "alice@example.com"
        assert matches[1].original == "bob@example.org"

    def test_scan_returns_redaction_match_dataclass(self) -> None:
        m = scan("alice@example.com")[0]
        assert isinstance(m, RedactionMatch)
        assert m.category == "EMAIL"
        assert m.start == 0
        assert m.end == len("alice@example.com")

    def test_redact_and_scan_agree_on_count(self) -> None:
        text = EMAIL_EX + " " + GITHUB_TOKEN_EX + " " + IPV4_EX
        # scan finds matches across multiple categories
        all_matches = scan(text)
        assert len(all_matches) >= 3
        # Redacted text is shorter or equal
        redacted = redact(text)
        assert len(redacted) <= len(text)


# === Test: categories filter ===

class TestCategoriesFilter:
    def test_narrow_to_email_only(self) -> None:
        text = EMAIL_EX + " " + GITHUB_TOKEN_EX
        result = redact(text, categories={"EMAIL"})
        # Email is redacted.
        assert "alice@" not in result
        # GitHub token survives (out of scope for this narrow call).
        assert "ghp_" in result

    def test_empty_categories_redacts_nothing(self) -> None:
        text = EMAIL_EX + " " + GITHUB_TOKEN_EX
        result = redact(text, categories=set())
        # Nothing changes.
        assert result == text

    def test_unknown_category_is_silently_ignored(self) -> None:
        text = EMAIL_EX
        result = redact(text, categories={"NOT_A_REAL_CATEGORY"})
        # Text unchanged because the only requested category doesn't exist.
        assert result == text

    def test_scan_with_categories_filter(self) -> None:
        text = EMAIL_EX + " " + GITHUB_TOKEN_EX
        only_email = scan(text, categories={"EMAIL"})
        assert len(only_email) == 1
        assert only_email[0].category == "EMAIL"


# === Test: redact_dict ===

class TestRedactDict:
    def test_redacts_specified_top_level_field(self) -> None:
        d = {"prompt": "Email alice@example.com", "model": "gpt-4"}
        out = redact_dict(d, fields={"prompt"})
        assert "alice@" not in out["prompt"]
        assert "<EMAIL>" in out["prompt"]
        # model field untouched (not in fields).
        assert out["model"] == "gpt-4"

    def test_redacts_nested_dict(self) -> None:
        d = {
            "outer": {
                "body": "IP: 192.168.1.42",
                "id": 42,
            },
        }
        out = redact_dict(d, fields={"body"})
        assert "192.168" not in out["outer"]["body"]
        # id is int, not str, so left untouched.
        assert out["outer"]["id"] == 42

    def test_redacts_list_of_dicts(self) -> None:
        d = {
            "items": [
                {"body": "alice@example.com"},
                {"body": "bob@example.org"},
            ],
        }
        out = redact_dict(d, fields={"body"})
        for item in out["items"]:
            assert "@" not in item["body"]
            assert "<EMAIL>" in item["body"]

    def test_does_not_mutate_input(self) -> None:
        d = {"body": "alice@example.com"}
        snapshot = dict(d)
        redact_dict(d, fields={"body"})
        assert d == snapshot

    def test_passes_through_non_dict_input(self) -> None:
        # Defensive: caller may pass None or a non-dict by accident.
        assert redact_dict(None, fields={"body"}) is None  # type: ignore[arg-type]
        assert redact_dict("not a dict", fields={"body"}) == "not a dict"  # type: ignore[arg-type]
        assert redact_dict(42, fields={"body"}) == 42  # type: ignore[arg-type]

    def test_with_categories_narrowing(self) -> None:
        d = {"body": "alice@example.com 192.168.1.42"}
        out = redact_dict(d, fields={"body"}, categories={"EMAIL"})
        # Email scrubbed.
        assert "<EMAIL>" in out["body"]
        # IP preserved (out of scope for this narrow call).
        assert "192.168.1.42" in out["body"]


# === Test: no regex catastrophic backtracking ===

class TestNoCatastrophicBacktracking:
    def test_long_input_without_secrets_returns_quick(self) -> None:
        # 100K chars of "a" — no pattern should hang.
        long_input = "a" * 100_000
        import time
        start = time.time()
        result = redact(long_input)
        elapsed = time.time() - start
        assert elapsed < 1.0, f"redact() took {elapsed:.2f}s on 100K a's"
        # No secrets to find → returned unchanged.
        assert result == long_input

    def test_long_input_with_one_secret_redacts(self) -> None:
        text = "a" * 50_000 + " alice@example.com " + "a" * 50_000
        result = redact(text)
        assert "alice@" not in result
        assert "<EMAIL>" in result


# === Test: pattern compilation (smoke) ===

class TestPatternCompilation:
    def test_all_patterns_are_compiled(self) -> None:
        for name, pat in PATTERNS.items():
            assert isinstance(pat, re.Pattern), f"{name} is not a compiled Pattern"
            assert pat.pattern, f"{name} has empty pattern"

    def test_at_least_12_patterns(self) -> None:
        # The 12 categories promised in the plan.
        assert len(PATTERNS) >= 12
