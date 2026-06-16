"""Phase 4.0: Tests for HookContext + HookDecision + HookAggregate."""
from __future__ import annotations

import pytest

from harness.hooks import (
    HookAggregate,
    HookContext,
    HookDecision,
    new_request_id,
)


class TestHookContext:
    """HookContext is a frozen dataclass with payload + event metadata."""

    def test_minimal_construction(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "read_file"},
        )
        assert ctx.event == "PreToolUse"
        assert ctx.session_id == "s1"
        assert ctx.payload == {"tool_name": "read_file"}
        assert ctx.ts > 0  # default_factory=time.time

    def test_with_payload_replaces_dict(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "read_file"},
        )
        new_ctx = ctx.with_payload({"tool_name": "write_file", "args": "..."})
        assert new_ctx.payload == {"tool_name": "write_file", "args": "..."}
        # Original is unchanged (frozen).
        assert ctx.payload == {"tool_name": "read_file"}

    def test_with_event_advances_stack(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={},
        )
        new_ctx = ctx.with_event("PostToolUse")
        assert new_ctx.event == "PostToolUse"
        assert new_ctx.event_stack == ("PreToolUse",)
        assert new_ctx.recursion_depth == 1

    def test_with_event_preserves_other_fields(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="agent-1",
            payload={"k": "v"},
            request_id="r1",
        )
        new_ctx = ctx.with_event("PostToolUse")
        assert new_ctx.session_id == "s1"
        assert new_ctx.agent_id == "agent-1"
        assert new_ctx.payload == {"k": "v"}
        assert new_ctx.request_id == "r1"

    def test_frozen_dataclass(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={},
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            ctx.event = "PostToolUse"  # type: ignore[misc]


class TestHookDecision:
    """HookDecision captures the result of a single hook."""

    def test_allow_default(self) -> None:
        d = HookDecision(decision="allow", hook_id="builtin.log")
        assert d.decision == "allow"
        assert d.hook_id == "builtin.log"
        assert d.output == {}
        assert d.error == ""
        assert d.duration_ms == 0.0

    def test_block_with_reason(self) -> None:
        d = HookDecision(
            decision="block",
            hook_id="block_dangerous",
            output={"reason": "rm -rf /"},
        )
        assert d.decision == "block"
        assert d.output["reason"] == "rm -rf /"

    def test_to_dict(self) -> None:
        d = HookDecision(decision="block", hook_id="x", output={"r": "y"})
        assert d.to_dict() == {
            "decision": "block",
            "hook_id": "x",
            "duration_ms": 0.0,
            "output": {"r": "y"},
            "error": "",
        }

    def test_from_dict_roundtrip(self) -> None:
        original = HookDecision(
            decision="modify",
            hook_id="x",
            duration_ms=12.5,
            output={"k": "v"},
            error="warn",
        )
        data = original.to_dict()
        restored = HookDecision.from_dict(data)
        assert restored.decision == original.decision
        assert restored.hook_id == original.hook_id
        assert restored.duration_ms == original.duration_ms
        assert restored.output == original.output
        assert restored.error == original.error

    def test_decision_literal_types(self) -> None:
        """Only 3 valid decision values."""
        for d in ("allow", "block", "modify"):
            HookDecision(decision=d, hook_id="x")  # type: ignore[arg-type]


class TestHookAggregate:
    """HookAggregate combines decisions from multiple hooks."""

    def test_construction(self) -> None:
        agg = HookAggregate(
            final_decision="allow",
            decisions=(),
        )
        assert agg.final_decision == "allow"
        assert agg.decisions == ()
        assert agg.final_payload == {}
        assert agg.blocked_by == ""

    def test_to_dict(self) -> None:
        d1 = HookDecision(decision="allow", hook_id="h1")
        d2 = HookDecision(decision="block", hook_id="h2", output={"r": "x"})
        agg = HookAggregate(
            final_decision="block",
            decisions=(d1, d2),
            final_payload={},
            blocked_by="h2",
        )
        out = agg.to_dict()
        assert out["final_decision"] == "block"
        assert out["blocked_by"] == "h2"
        assert len(out["decisions"]) == 2
        assert out["decisions"][0]["hook_id"] == "h1"


class TestRequestId:
    """new_request_id generates short unique ids."""

    def test_returns_string(self) -> None:
        rid = new_request_id()
        assert isinstance(rid, str)
        assert len(rid) == 12  # 12 hex chars

    def test_unique(self) -> None:
        ids = {new_request_id() for _ in range(100)}
        assert len(ids) == 100  # all unique
