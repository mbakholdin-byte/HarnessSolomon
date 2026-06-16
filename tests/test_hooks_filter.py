"""Phase 4.0: Tests for the filter_chain (match_glob)."""
from __future__ import annotations

import pytest

from harness.hooks.filter_chain import (
    _match_pattern,
    matches_filter_chain,
    parse_filter_chain,
)


class TestParseFilterChain:
    """parse_filter_chain parses a settings string into (field, pattern) pairs."""

    def test_empty(self) -> None:
        assert parse_filter_chain("") == []

    def test_single_rule(self) -> None:
        assert parse_filter_chain("event=PreToolUse") == [("event", "PreToolUse")]

    def test_multiple_rules(self) -> None:
        result = parse_filter_chain("event=PreToolUse,session_id=s-1,tool_name=read_*")
        assert result == [
            ("event", "PreToolUse"),
            ("session_id", "s-1"),
            ("tool_name", "read_*"),
        ]

    def test_strips_whitespace(self) -> None:
        result = parse_filter_chain(" event = PreToolUse , session_id = s-1 ")
        assert result == [("event", "PreToolUse"), ("session_id", "s-1")]

    def test_skips_empty_segments(self) -> None:
        result = parse_filter_chain("event=PreToolUse,,,tool_name=read_file")
        assert result == [("event", "PreToolUse"), ("tool_name", "read_file")]

    def test_payload_field(self) -> None:
        result = parse_filter_chain("payload.tool_name=write_file")
        assert result == [("payload.tool_name", "write_file")]


class TestMatchPattern:
    """_match_pattern handles globs + negation."""

    def test_literal_match(self) -> None:
        assert _match_pattern("PreToolUse", "PreToolUse") is True

    def test_literal_mismatch(self) -> None:
        assert _match_pattern("PostToolUse", "PreToolUse") is False

    def test_wildcard_match(self) -> None:
        assert _match_pattern("read_file", "read_*") is True
        assert _match_pattern("read_anything", "read_*") is True
        assert _match_pattern("write_file", "read_*") is False

    def test_negation(self) -> None:
        assert _match_pattern("read_file", "!write_*") is True
        assert _match_pattern("write_file", "!write_*") is False

    def test_empty_pattern_matches_all(self) -> None:
        assert _match_pattern("anything", "") is True


class TestMatchesFilterChain:
    """matches_filter_chain combines rules with AND semantics."""

    def test_empty_chain_matches_all(self) -> None:
        assert matches_filter_chain(
            "",
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={},
        ) is True

    def test_single_rule_match(self) -> None:
        assert matches_filter_chain(
            "event=PreToolUse",
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={},
        ) is True

    def test_single_rule_no_match(self) -> None:
        assert matches_filter_chain(
            "event=PreToolUse",
            event="PostToolUse",
            session_id="s1",
            agent_id="",
            payload={},
        ) is False

    def test_multiple_rules_and(self) -> None:
        spec = "event=PreToolUse,tool_name=read_*"
        # Both match.
        assert (
            matches_filter_chain(
                spec,
                event="PreToolUse",
                session_id="s1",
                agent_id="",
                payload={"tool_name": "read_file"},
            )
            is True
        )
        # First matches, second doesn't.
        assert (
            matches_filter_chain(
                spec,
                event="PreToolUse",
                session_id="s1",
                agent_id="",
                payload={"tool_name": "write_file"},
            )
            is False
        )

    def test_payload_field(self) -> None:
        spec = "payload.tool_name=write_file"
        assert (
            matches_filter_chain(
                spec,
                event="PreToolUse",
                session_id="s1",
                agent_id="",
                payload={"tool_name": "write_file"},
            )
            is True
        )

    def test_negation_in_chain(self) -> None:
        spec = "tool_name=!rm"
        assert (
            matches_filter_chain(
                spec,
                event="PreToolUse",
                session_id="s1",
                agent_id="",
                payload={"tool_name": "read_file"},
            )
            is True
        )
        assert (
            matches_filter_chain(
                spec,
                event="PreToolUse",
                session_id="s1",
                agent_id="",
                payload={"tool_name": "rm"},
            )
            is False
        )
