"""Phase 4.0: Tests for HookAuditSink + audit integration in HookRunner."""
from __future__ import annotations

import json
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
from harness.hooks.audit import HookAuditSink


async def _allow_hook(ctx: HookContext) -> HookDecision:
    return HookDecision(decision="allow", hook_id="allow")


class TestHookAuditSink:
    """HookAuditSink writes JSONL audit lines (Plan B2)."""

    def test_record_creates_file(self, tmp_path: Path) -> None:
        sink = HookAuditSink(tmp_path)
        from harness.hooks import HookAggregate

        agg = HookAggregate(
            final_decision="allow",
            decisions=(HookDecision(decision="allow", hook_id="h1"),),
        )
        sink.record(
            aggregate=agg,
            event="PreToolUse",
            session_id="s1",
            agent_id="a1",
        )
        # File should be created.
        files = list(tmp_path.glob("hooks-*.ndjson"))
        assert len(files) == 1
        # File should have one line.
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event"] == "PreToolUse"
        assert data["session_id"] == "s1"
        assert data["agent_id"] == "a1"
        assert data["aggregate"]["final_decision"] == "allow"
        assert data["aggregate"]["decisions"][0]["hook_id"] == "h1"

    def test_record_appends_multiple(self, tmp_path: Path) -> None:
        sink = HookAuditSink(tmp_path)
        from harness.hooks import HookAggregate

        agg = HookAggregate(final_decision="allow", decisions=())
        for i in range(3):
            sink.record(
                aggregate=agg,
                event="PreToolUse",
                session_id=f"s-{i}",
                agent_id="a1",
            )
        files = list(tmp_path.glob("hooks-*.ndjson"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    def test_record_creates_dir(self, tmp_path: Path) -> None:
        sink = HookAuditSink(tmp_path / "nested" / "deeper")
        from harness.hooks import HookAggregate

        agg = HookAggregate(final_decision="allow", decisions=())
        sink.record(
            aggregate=agg, event="X", session_id="s", agent_id=""
        )
        assert (tmp_path / "nested" / "deeper").is_dir()
        assert any((tmp_path / "nested" / "deeper").glob("hooks-*.ndjson"))

    def test_tail_returns_last_n(self, tmp_path: Path) -> None:
        sink = HookAuditSink(tmp_path)
        from harness.hooks import HookAggregate

        agg = HookAggregate(final_decision="allow", decisions=())
        for i in range(10):
            sink.record(
                aggregate=agg,
                event="PreToolUse",
                session_id=f"s-{i}",
                agent_id="",
            )
        tail = sink.tail(n=3)
        assert len(tail) == 3
        assert tail[-1]["session_id"] == "s-9"
        assert tail[0]["session_id"] == "s-7"

    def test_tail_empty_when_no_file(self, tmp_path: Path) -> None:
        sink = HookAuditSink(tmp_path)
        assert sink.tail(n=10) == []

    def test_record_handles_unicode(self, tmp_path: Path) -> None:
        sink = HookAuditSink(tmp_path)
        from harness.hooks import HookAggregate

        agg = HookAggregate(final_decision="allow", decisions=())
        sink.record(
            aggregate=agg,
            event="PreToolUse",
            session_id="s-русский",
            agent_id="a-1",
        )
        files = list(tmp_path.glob("hooks-*.ndjson"))
        data = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert data["session_id"] == "s-русский"


class TestHookRunnerWithAudit:
    """HookRunner with audit_sink writes audit lines automatically."""

    async def test_runner_writes_audit(self, tmp_path: Path) -> None:
        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h1",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_allow_hook,
            )
        )
        sink = HookAuditSink(tmp_path)
        runner = HookRunner(registry, audit_sink=sink)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="a1", payload={}
        )
        await runner.fire(ctx)
        files = list(tmp_path.glob("hooks-*.ndjson"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert data["event"] == "PreToolUse"
        assert data["session_id"] == "s1"

    async def test_runner_no_audit_sink(self) -> None:
        """No audit_sink → no error, no file written."""
        registry = HookRegistry()
        runner = HookRunner(registry)  # no audit
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "allow"

    async def test_audit_records_block_decision(self, tmp_path: Path) -> None:
        async def _block(ctx: HookContext) -> HookDecision:
            return HookDecision(decision="block", hook_id="h-block")

        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="h-block",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_block,
            )
        )
        sink = HookAuditSink(tmp_path)
        runner = HookRunner(registry, audit_sink=sink)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        agg = await runner.fire(ctx)
        assert agg.final_decision == "block"
        files = list(tmp_path.glob("hooks-*.ndjson"))
        data = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert data["aggregate"]["final_decision"] == "block"
        assert data["aggregate"]["blocked_by"] == "h-block"
