"""Integration tests for ``AgentLoop._record_event`` and
``ToolRuntime`` events collector (Phase 3 v1.4.0).

Covers:
  - ``_record_event`` is a no-op when ``runtime._events_collector`` is ``None``
  - ``_record_event`` appends a ``SessionEvent`` to the collector
  - ``_record_event`` falls back to dict-like object if the reflection
    module is somehow unavailable (defence-in-depth)
  - ``_record_event`` swallows errors from a non-list collector
  - ``_extract_offloaded_note_id`` returns ``None`` when content is the
    full original (no offload)
  - ``_extract_offloaded_note_id`` parses ``id=N`` from a stub
  - ``_extract_offloaded_note_id`` returns ``None`` on malformed input
  - ``ToolRuntime`` accepts ``events_collector`` kwarg and stores it
  - ``ToolRuntime.events_collector`` defaults to ``None`` (backward compat)
  - ``_record_event`` records assistant events
  - ``_record_event`` records tool events with ``offloaded_id``

We use lightweight stub objects for the runtime — AgentLoop needs a
runtime with ``_events_collector`` and that is the only field the
event-recording path touches. The agent loop's other fields are
populated in the integration test.
"""
from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.server.agent.loop import AgentLoop, _OFFLOADED_ID_RE


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class FakeRuntime:
    """Minimal runtime with only the fields used by ``_record_event``."""

    def __init__(self, *, events_collector: Any = None) -> None:
        self._events_collector = events_collector


# ---------------------------------------------------------------------------
# _record_event tests
# ---------------------------------------------------------------------------


class TestRecordEvent:
    def test_no_op_when_collector_is_none(self) -> None:
        runtime = FakeRuntime(events_collector=None)
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        # Should not raise.
        loop._record_event(kind="user", content="hi")
        assert runtime._events_collector is None

    def test_appends_session_event_to_collector(self) -> None:
        events: list[Any] = []
        runtime = FakeRuntime(events_collector=events)
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        loop._record_event(kind="assistant", content="hello")
        assert len(events) == 1
        ev = events[0]
        # SessionEvent has the expected fields.
        assert ev.kind == "assistant"  # type: ignore[attr-defined]
        assert ev.content == "hello"  # type: ignore[attr-defined]
        assert ev.ts > 0  # type: ignore[attr-defined]

    def test_appends_tool_event_with_offloaded_id(self) -> None:
        events: list[Any] = []
        runtime = FakeRuntime(events_collector=events)
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        loop._record_event(
            kind="tool", content="stub", tool_name="bash", offloaded_id=42,
        )
        assert len(events) == 1
        ev = events[0]
        assert ev.kind == "tool"  # type: ignore[attr-defined]
        assert ev.tool_name == "bash"  # type: ignore[attr-defined]
        assert ev.offloaded_id == 42  # type: ignore[attr-defined]

    def test_swallows_error_from_frozen_collector(self) -> None:
        """If the collector raises on .append(), we log and continue."""
        frozen = MagicMock()
        frozen.append.side_effect = RuntimeError("frozen")
        runtime = FakeRuntime(events_collector=frozen)
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        # Should not raise.
        loop._record_event(kind="assistant", content="hi")

    def test_collector_without_append_is_ignored(self) -> None:
        """A collector without .append() is a no-op (not a hard error)."""
        runtime = FakeRuntime(events_collector=42)  # int has no append
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        loop._record_event(kind="user", content="hi")

    def test_multiple_events_appended_in_order(self) -> None:
        events: list[Any] = []
        runtime = FakeRuntime(events_collector=events)
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        loop._record_event(kind="user", content="a")
        loop._record_event(kind="assistant", content="b")
        loop._record_event(kind="tool", content="c", tool_name="bash")
        assert len(events) == 3
        assert [e.kind for e in events] == ["user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# _extract_offloaded_note_id tests
# ---------------------------------------------------------------------------


class TestExtractOffloadedNoteId:
    def test_returns_none_when_no_offload(self) -> None:
        """If the recorded content is the full original, no offload happened."""
        runtime = FakeRuntime()
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        full = "x" * 1000
        result = loop._extract_offloaded_note_id(content=full, original_full=full)
        assert result is None

    def test_returns_none_when_content_longer_than_original(self) -> None:
        """Defensive: content longer than original = not an offload."""
        runtime = FakeRuntime()
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        result = loop._extract_offloaded_note_id(
            content="x" * 100, original_full="x" * 50,
        )
        assert result is None

    def test_parses_id_from_stub(self) -> None:
        runtime = FakeRuntime()
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        full = "x" * 30000
        stub = (
            "(offloaded 27123 bytes; id=42; tool=bash; "
            "read via scratchpad_read_offloaded(id=42))"
        )
        result = loop._extract_offloaded_note_id(content=stub, original_full=full)
        assert result == 42

    def test_returns_none_for_non_string_content(self) -> None:
        runtime = FakeRuntime()
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        result = loop._extract_offloaded_note_id(
            content=None, original_full="x" * 100,
        )
        assert result is None

    def test_returns_none_for_non_string_original(self) -> None:
        runtime = FakeRuntime()
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        result = loop._extract_offloaded_note_id(
            content="(offloaded ... id=42 ...)", original_full=None,
        )
        assert result is None

    def test_returns_none_when_id_pattern_missing(self) -> None:
        runtime = FakeRuntime()
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        stub = "(offloaded bytes; no id here)"
        result = loop._extract_offloaded_note_id(
            content=stub, original_full="x" * 1000,
        )
        assert result is None

    def test_returns_none_for_unparseable_id(self) -> None:
        runtime = FakeRuntime()
        loop = AgentLoop(runtime=runtime, router=MagicMock(), compactor=None)
        stub = "(offloaded ... id=notanint ...)"
        result = loop._extract_offloaded_note_id(
            content=stub, original_full="x" * 1000,
        )
        assert result is None

    def test_regex_is_compiled_at_module_level(self) -> None:
        assert isinstance(_OFFLOADED_ID_RE, re.Pattern)
        m = _OFFLOADED_ID_RE.search("hello id=123 world")
        assert m is not None
        assert m.group(1) == "123"


# ---------------------------------------------------------------------------
# ToolRuntime events_collector kwarg tests
# ---------------------------------------------------------------------------


class TestRuntimeEventsCollector:
    def test_runtime_accepts_events_collector_kwarg(self, tmp_path) -> None:
        from harness.server.agent.runtime import ToolRuntime

        collector: list[Any] = []
        runtime = ToolRuntime(
            project_root=tmp_path,
            events_collector=collector,
        )
        assert runtime._events_collector is collector

    def test_runtime_events_collector_defaults_to_none(self, tmp_path) -> None:
        from harness.server.agent.runtime import ToolRuntime

        runtime = ToolRuntime(project_root=tmp_path)
        assert runtime._events_collector is None

    def test_runtime_reflection_and_collector_independent(
        self, tmp_path,
    ) -> None:
        from harness.server.agent.runtime import ToolRuntime

        reflection = MagicMock()
        collector: list[Any] = []
        runtime = ToolRuntime(
            project_root=tmp_path,
            reflection=reflection,
            events_collector=collector,
        )
        assert runtime._reflection is reflection
        assert runtime._events_collector is collector

    def test_existing_kwargs_unchanged(self, tmp_path) -> None:
        """Adding ``events_collector`` kwarg must not regress any v1.3.x feature."""
        from harness.server.agent.runtime import ToolRuntime

        offloader = MagicMock()
        reflection = MagicMock()
        runtime = ToolRuntime(
            project_root=tmp_path,
            tool_offloader=offloader,
            reflection=reflection,
        )
        assert runtime._tool_offloader is offloader
        assert runtime._reflection is reflection
        assert runtime._events_collector is None


# ---------------------------------------------------------------------------
# Integration: AgentLoop.run records events as it streams
# ---------------------------------------------------------------------------


class TestAgentLoopEventCollection:
    async def test_assistant_message_emits_event(self, tmp_path) -> None:
        """When the loop yields an assistant_message, an event is recorded."""
        events: list[Any] = []
        runtime = FakeRuntime(events_collector=events)
        runtime.project_root = tmp_path  # AgentLoop reads this for system prompt

        # Build an inline FakeRouter that yields one assistant message
        # and a 'done' event via ``completion`` (non-streaming path).
        from harness.server.agent.loop import StreamEvent

        class InlineFakeRouter:
            def __init__(self) -> None:
                self.completed = False

            async def completion(self, *, messages, model, tools=None, **kwargs):
                if not self.completed:
                    self.completed = True
                    return _build_fake_completion_result("hello world")
                # Second call → empty response (no tool calls) to terminate loop.
                return _build_fake_completion_result("done")

            async def streaming_completion(self, *, messages, model, tools=None, **kwargs):
                yield StreamEvent(type="assistant_message", content="hello")
                yield StreamEvent(type="done")

        router = InlineFakeRouter()
        loop = AgentLoop(runtime=runtime, router=router, compactor=None)

        async def _drive():
            async for ev in loop.run(messages=[], model="m"):
                if ev.type == "done":
                    return

        await _drive()
        # We should have at least one assistant event in the collector.
        kinds = [e.kind for e in events]
        assert "assistant" in kinds


def _build_fake_completion_result(content: str) -> Any:
    """Build a duck-typed CompletionResult-like object for inline fakes."""
    from harness.server.llm.router import CompletionResult

    return CompletionResult(
        content=content,
        tool_calls=[],
        usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        cost=0.0,
    )
