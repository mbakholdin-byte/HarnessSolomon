"""Tests for AgentLoop tool-result offload trigger (Phase 3 v1.3.1, Step 2)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.config import Settings
from harness.server.agent.loop import AgentLoop
from harness.server.agent.runtime import ToolRuntime
from harness.server.llm.router import CompletionResult, StreamEvent


# === Fakes ===

class FakeRouter:
    """Fake LLMRouter — programmable sequence of responses."""

    def __init__(self, scripted_responses: list[CompletionResult]) -> None:
        self.scripted_responses = scripted_responses
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    async def completion(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        self.calls.append(
            {"messages": list(messages), "model": model, "tools": tools}
        )
        idx = min(self.call_count, len(self.scripted_responses) - 1)
        result = self.scripted_responses[idx]
        self.call_count += 1
        return result


class FakeOffloader:
    """Minimal ToolOffloader double — records every call."""

    def __init__(
        self,
        *,
        should_offload_result: bool = True,
        offload_result: int | None = 1,
        session_id: str = "fake-sess",
        timeout_ms: int = 2000,
        offload_delay_s: float = 0.0,
    ) -> None:
        self.should_calls: list[str] = []
        self.offload_calls: list[dict[str, Any]] = []
        self.stub_calls: list[dict[str, Any]] = []
        self._should = should_offload_result
        self._result = offload_result
        self._scratchpad = type(
            "FakeSp", (), {"_session_id": session_id},
        )()
        self._settings = Settings(tool_offload_max_ms=timeout_ms)
        self._delay = offload_delay_s

    def should_offload(self, content: str) -> bool:
        self.should_calls.append(content)
        return self._should

    async def offload(
        self,
        content: str,
        *,
        tool_name: str,
        session_id: str,
        tool_call_id: str | None = None,
    ) -> int | None:
        self.offload_calls.append(
            {
                "content": content, "tool_name": tool_name,
                "session_id": session_id, "tool_call_id": tool_call_id,
            }
        )
        if self._delay > 0:
            import asyncio
            await asyncio.sleep(self._delay)
        return self._result

    def build_stub(
        self, content: str, *, note_id: int, tool_name: str,
    ) -> str:
        self.stub_calls.append(
            {"content": content, "note_id": note_id, "tool_name": tool_name},
        )
        return f"[STUB id={note_id} tool={tool_name}]"


# === Helpers ===

def _make_runtime(
    tmp_path: Path,
    *,
    offloader: Any = None,
) -> ToolRuntime:
    return ToolRuntime(tmp_path, tool_offloader=offloader)


def _tool_call_response(name: str = "read_file") -> CompletionResult:
    return CompletionResult(
        content="",
        tool_calls=[{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps({"path": "small.txt"}),
            },
        }],
        usage={"prompt_tokens": 0, "completion_tokens": 0},
        cost=0.0,
    )


def _final_response() -> CompletionResult:
    return CompletionResult(
        content="done",
        tool_calls=[],
        usage={"prompt_tokens": 0, "completion_tokens": 0},
        cost=0.0,
    )


def _first_tool_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for m in messages:
        if m.get("role") == "tool":
            return m
    return None


async def _drive(
    loop: AgentLoop,
    *,
    initial_user: str = "do something",
) -> list[StreamEvent]:
    """Drive the loop to completion and return the emitted events.

    The loop rebinds the messages list inside its body (via
    ``redact_dict`` at line 236 of loop.py) so the original list the
    caller passed in is not mutated past the rebind. The events we
    emit are the only reliable surface for asserting what the LLM
    actually saw.
    """
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user},
    ]
    events: list[StreamEvent] = []
    async for ev in loop.run(messages, model="qwen3:8b", stream=False):
        events.append(ev)
    return events


def _first_tool_result(events: list[StreamEvent]) -> StreamEvent | None:
    for e in events:
        if e.type == "tool_result":
            return e
    return None


# === Trigger ===

class TestLoopOffloadTrigger:
    async def test_over_threshold_replaces_with_stub(
        self, tmp_path: Path,
    ) -> None:
        tmp_path.joinpath("small.txt").write_text("hi", encoding="utf-8")
        router = FakeRouter([_tool_call_response(), _final_response()])
        offloader = FakeOffloader(
            should_offload_result=True, offload_result=1,
        )
        runtime = _make_runtime(tmp_path, offloader=offloader)
        loop = AgentLoop(runtime, router, max_iterations=3)  # type: ignore[arg-type]

        events = await _drive(loop)

        assert offloader.should_calls, "should_offload was not called"
        assert offloader.offload_calls, "offload was not called"
        assert offloader.stub_calls, "build_stub was not called"
        tool_event = _first_tool_result(events)
        assert tool_event is not None
        assert tool_event.content == "[STUB id=1 tool=read_file]"

    async def test_under_threshold_keeps_full_content(
        self, tmp_path: Path,
    ) -> None:
        tmp_path.joinpath("small.txt").write_text("hi", encoding="utf-8")
        router = FakeRouter([_tool_call_response(), _final_response()])
        offloader = FakeOffloader(should_offload_result=False)
        runtime = _make_runtime(tmp_path, offloader=offloader)
        loop = AgentLoop(runtime, router, max_iterations=3)  # type: ignore[arg-type]

        events = await _drive(loop)

        assert offloader.should_calls
        assert not offloader.offload_calls
        tool_event = _first_tool_result(events)
        assert tool_event is not None
        assert tool_event.content == "hi"

    async def test_no_offloader_keeps_full_content(
        self, tmp_path: Path,
    ) -> None:
        tmp_path.joinpath("small.txt").write_text("hi", encoding="utf-8")
        router = FakeRouter([_tool_call_response(), _final_response()])
        runtime = _make_runtime(tmp_path, offloader=None)
        loop = AgentLoop(runtime, router, max_iterations=3)  # type: ignore[arg-type]

        events = await _drive(loop)

        tool_event = _first_tool_result(events)
        assert tool_event is not None
        assert tool_event.content == "hi"

    async def test_offload_failure_keeps_full_content(
        self, tmp_path: Path,
    ) -> None:
        tmp_path.joinpath("small.txt").write_text("hi", encoding="utf-8")
        router = FakeRouter([_tool_call_response(), _final_response()])
        offloader = FakeOffloader(
            should_offload_result=True, offload_result=None,
        )
        runtime = _make_runtime(tmp_path, offloader=offloader)
        loop = AgentLoop(runtime, router, max_iterations=3)  # type: ignore[arg-type]

        events = await _drive(loop)

        tool_event = _first_tool_result(events)
        assert tool_event is not None
        assert tool_event.content == "hi"


# === Timeout ===

class TestLoopOffloadTimeout:
    async def test_timeout_keeps_full_content(
        self, tmp_path: Path,
    ) -> None:
        """When offload() takes longer than the configured timeout,
        the loop keeps the full content (fail-open)."""
        tmp_path.joinpath("small.txt").write_text("hi", encoding="utf-8")
        router = FakeRouter([_tool_call_response(), _final_response()])
        # 200ms timeout + 500ms offload delay → guaranteed timeout.
        offloader = FakeOffloader(
            should_offload_result=True,
            offload_result=1,
            timeout_ms=200,
            offload_delay_s=0.5,
        )
        runtime = _make_runtime(tmp_path, offloader=offloader)
        loop = AgentLoop(runtime, router, max_iterations=3)  # type: ignore[arg-type]

        events = await _drive(loop)

        tool_event = _first_tool_result(events)
        assert tool_event is not None
        assert tool_event.content == "hi"


# === Session ID resolution ===

class TestLoopOffloadSessionId:
    async def test_session_id_resolved_from_runtime(
        self, tmp_path: Path,
    ) -> None:
        """The offloader.offload() call must receive the session_id
        stored on the offloader's inner scratchpad (via getattr chain)."""
        tmp_path.joinpath("small.txt").write_text("hi", encoding="utf-8")
        router = FakeRouter([_tool_call_response(), _final_response()])
        offloader = FakeOffloader(
            should_offload_result=True,
            offload_result=42,
            session_id="my-session-id-123",
        )
        runtime = _make_runtime(tmp_path, offloader=offloader)
        loop = AgentLoop(runtime, router, max_iterations=3)  # type: ignore[arg-type]

        await _drive(loop)

        assert offloader.offload_calls
        call = offloader.offload_calls[0]
        assert call["session_id"] == "my-session-id-123"
        assert call["tool_call_id"] == "call_1"


# === Stub format ===

class TestLoopOffloadStubFormat:
    async def test_stub_passes_note_id_and_tool_name(
        self, tmp_path: Path,
    ) -> None:
        """build_stub() must be called with the note id returned by
        offload() and the original tool name."""
        tmp_path.joinpath("small.txt").write_text("hi", encoding="utf-8")
        router = FakeRouter([_tool_call_response(), _final_response()])
        offloader = FakeOffloader(
            should_offload_result=True, offload_result=99,
        )
        runtime = _make_runtime(tmp_path, offloader=offloader)
        loop = AgentLoop(runtime, router, max_iterations=3)  # type: ignore[arg-type]

        await _drive(loop)

        assert offloader.stub_calls
        call = offloader.stub_calls[0]
        assert call["note_id"] == 99
        assert call["tool_name"] == "read_file"
