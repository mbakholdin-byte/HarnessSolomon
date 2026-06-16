"""Phase 4.0: Smoke tests for examples in docs/hooks.md.

Each test exercises one code snippet from the docs to ensure it
actually runs. If these fail, the docs are lying.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.hooks import (
    EventType,
    HookContext,
    HookDecision,
    HookRegistry,
    HookRunner,
    HookSpec,
)


async def _allow_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(decision="allow", hook_id="docs-allow")


async def _block_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(
        decision="block", hook_id="docs-block",
        output={"reason": "policy violation"},
    )


class TestBuiltinExample:
    """Docs §9.1: minimal builtin example (log + block_dangerous)."""

    async def test_minimal_builtin_allow(self) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="docs.builtin.allow",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_allow_hook,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="a1",
            payload={"tool_name": "read_file", "arguments": {"path": "x"}},
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"
        assert agg.decisions[0].hook_id == "docs.builtin.allow"


class TestSubprocessExample:
    """Docs §9.2: subprocess (allow via exit 0, block via exit 2)."""

    @pytest.fixture
    def script_path(self, tmp_path: Path) -> Path:
        """Write a docs-style subprocess hook to disk."""
        script = tmp_path / "audit_hook.py"
        script.write_text(
            "import json, sys\n"
            "ctx = json.load(sys.stdin)\n"
            "if 'rm -rf' in str(ctx.get('payload', {}).get('arguments', '')):\n"
            "    print('destructive', file=sys.stderr)\n"
            "    sys.exit(2)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        return script

    async def test_subprocess_allow(
        self, script_path: Path, tmp_path: Path
    ) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="docs.subprocess.allow",
                event=EventType.PRE_TOOL_USE,
                transport="subprocess",
                script_path=str(script_path),
                timeout_ms=2000,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="a1",
            payload={"tool_name": "read_file", "arguments": {"path": "x"}},
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"

    async def test_subprocess_block(self, script_path: Path) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="docs.subprocess.block",
                event=EventType.PRE_TOOL_USE,
                transport="subprocess",
                script_path=str(script_path),
                timeout_ms=2000,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="a1",
            payload={"tool_name": "bash",
                     "arguments": {"command": "rm -rf /tmp/x"}},
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "block"
        assert agg.blocked_by == "docs.subprocess.block"


class TestModifyExample:
    """Docs §9.5: modify (redact PII in payload)."""

    async def test_modify_payload_replaced(self) -> None:
        """modify decision's output['payload'] is the new payload."""

        async def redact(ctx: HookContext) -> HookDecision:
            args = dict(ctx.payload.get("arguments", {}))
            text = args.get("text", "")
            if "4111111111111111" in text:
                args["text"] = text.replace("4111111111111111", "<CARD>")
                return HookDecision(
                    decision="modify", hook_id="docs.redact",
                    output={"payload": {
                        "tool_name": ctx.payload["tool_name"],
                        "arguments": args,
                    }},
                )
            return HookDecision(decision="allow", hook_id="docs.redact")

        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="docs.modify",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=redact,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="a1",
            payload={
                "tool_name": "write_file",
                "arguments": {"text": "card 4111111111111111 here"},
            },
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "modify"
        assert agg.final_payload["arguments"]["text"] == "card <CARD> here"


class TestAggregationExample:
    """Docs §3: aggregation — first block wins, last modify wins."""

    async def test_first_block_wins(self) -> None:
        async def _a(ctx: HookContext) -> HookDecision:
            return HookDecision(decision="allow", hook_id="a")
        async def _b(ctx: HookContext) -> HookDecision:
            return HookDecision(
                decision="block", hook_id="b",
                output={"reason": "b blocked"},
            )
        async def _c(ctx: HookContext) -> HookDecision:
            return HookDecision(decision="allow", hook_id="c")

        registry = HookRegistry()
        for fn, hid in [(_a, "a"), (_b, "b"), (_c, "c")]:
            await registry.register(
                HookSpec(
                    hook_id=f"agg.{hid}",
                    event=EventType.PRE_TOOL_USE,
                    transport="builtin",
                    callable=fn,
                    priority={"a": 1, "b": 2, "c": 3}[hid],
                )
            )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="a1", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "block"
        assert agg.blocked_by == "agg.b"  # b blocked, c after b was allow

    async def test_last_modify_wins(self) -> None:
        async def _m1(ctx: HookContext) -> HookDecision:
            return HookDecision(
                decision="modify", hook_id="m1",
                output={"payload": {"v": 1}},
            )
        async def _m2(ctx: HookContext) -> HookDecision:
            return HookDecision(
                decision="modify", hook_id="m2",
                output={"payload": {"v": 2}},
            )

        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="agg.m1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_m1,
                priority=1,
            )
        )
        await registry.register(
            HookSpec(
                hook_id="agg.m2",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_m2,
                priority=2,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="a1", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "modify"
        assert agg.final_payload == {"v": 2}  # last modify wins


class TestParseSpecExample:
    """Docs §6.2: parse_spec for 4 transport formats."""

    def test_parse_spec_4_formats(self) -> None:
        from harness.hooks.registry import parse_spec

        s1 = parse_spec("PreToolUse:builtin:log")
        assert s1.event == EventType.PRE_TOOL_USE
        assert s1.transport == "builtin"
        assert s1.hook_id == "user.builtin.log"

        s2 = parse_spec("PreToolUse:subprocess:/abs/hook.py:1000")
        assert s2.transport == "subprocess"
        assert s2.script_path == "/abs/hook.py"
        assert s2.timeout_ms == 1000

        s3 = parse_spec("PreToolUse:http:https://ex.com/h:2000")
        assert s3.transport == "http"
        assert s3.url == "https://ex.com/h"
        assert s3.timeout_ms == 2000
        assert s3.headers == {}

        s3b = parse_spec("PreToolUse:http:https://ex.com/h:Bearer abc")
        assert s3b.headers["Authorization"] == "Bearer abc"
        assert s3b.timeout_ms is None

        s4 = parse_spec("OnRoutingDecision:llm:qwen3:3000:Is it safe?")
        assert s4.transport == "llm"
        assert s4.model == "qwen3"
        assert s4.timeout_ms == 3000
        assert s4.prompt == "Is it safe?"

        # Note: model name cannot contain ':' (parser splits on first ':').
        # Use '-' or '/' for model versions: gpt-4o-mini, qwen3/8b, etc.
