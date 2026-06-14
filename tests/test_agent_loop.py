"""Tests for AgentLoop (Шаг 6, Phase 0).

Per TDD (RED → GREEN → REFACTOR): tests first, then minimal implementation.

The AgentLoop is responsible for:
  * iterating between LLM completions and tool executions,
  * capping the number of iterations (default 5),
  * yielding a stream of events the WebSocket layer can forward.

The tests use a FakeRouter (programmed responses) and a real ToolRuntime
bound to a temp project_root. We do NOT mock the runtime — the safety
layer must remain in the loop.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.server.agent.loop import AgentLoop
from harness.server.agent.runtime import ToolRuntime
from harness.server.agent.tools import TOOL_SCHEMAS
from harness.server.llm.router import CompletionResult, StreamEvent


# === Fakes ===

class FakeRouter:
    """Fake LLMRouter for tests — programmable sequence of responses.

    Records every call (messages, model, tools) so tests can assert on
    what was actually sent to the LLM layer.
    """

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
        if self.call_count >= len(self.scripted_responses):
            raise RuntimeError("FakeRouter: out of scripted responses")
        resp = self.scripted_responses[self.call_count]
        self.call_count += 1
        return resp


def _make_tool_call(
    call_id: str, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Build a tool_call dict in the format CompletionResult emits.

    CompletionResult normalises tool_calls to:
      {"id": ..., "type": "function",
       "function": {"name": ..., "arguments": <json string>}}
    """
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


# === Tests ===

async def test_loop_one_iteration_no_tools(tmp_path: Path) -> None:
    """Single iteration, no tool_calls → 1 assistant_message + done."""
    runtime = ToolRuntime(project_root=tmp_path)
    router = FakeRouter(
        scripted_responses=[
            CompletionResult(
                content="Hello, world!",
                tool_calls=None,
                usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                cost=0.0,
            )
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "hi"}], model="MiniMax-M2.7"
    ):
        events.append(ev)

    # Filter event types
    types = [ev.type for ev in events]
    assert types.count("assistant_message") == 1
    assert types.count("done") == 1
    # Last event must be 'done'
    assert events[-1].type == "done"
    # The single assistant message carries the content
    assert events[0].content == "Hello, world!"
    # Router was hit exactly once
    assert router.call_count == 1


async def test_loop_two_iterations_with_one_tool_call(tmp_path: Path) -> None:
    """Two iterations, 1 tool_call in the second → 2 assistant + 1 tool_result + done."""
    # Pre-create the file the LLM will ask to read.
    (tmp_path / "hello.txt").write_text("hi from file", encoding="utf-8")

    runtime = ToolRuntime(project_root=tmp_path)
    router = FakeRouter(
        scripted_responses=[
            # Iter 1: LLM asks to read a file.
            CompletionResult(
                content="Reading file...",
                tool_calls=[_make_tool_call("call_1", "read_file", {"path": "hello.txt"})],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            ),
            # Iter 2: LLM returns final answer (no tool calls).
            CompletionResult(
                content="The file says: hi from file",
                tool_calls=None,
                usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            ),
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "read hello.txt"}],
        model="MiniMax-M2.7",
    ):
        events.append(ev)

    types = [ev.type for ev in events]
    assert types.count("assistant_message") == 2
    assert types.count("tool_result") == 1
    assert types.count("done") == 1
    # done must still be the LAST event.
    assert events[-1].type == "done"

    # The tool_result event must carry the file content
    tool_results = [ev for ev in events if ev.type == "tool_result"]
    assert len(tool_results) == 1
    assert "hi from file" in tool_results[0].content

    # Router was called twice
    assert router.call_count == 2


async def test_loop_caps_at_max_iterations(tmp_path: Path) -> None:
    """Tool-call on every iteration → 5 assistant + 5 tool_result + error + done."""
    runtime = ToolRuntime(project_root=tmp_path)
    # 5 responses, each with a tool call to a tool we'll let succeed (glob).
    router = FakeRouter(
        scripted_responses=[
            CompletionResult(
                content=f"iter {i}",
                tool_calls=[_make_tool_call(f"call_{i}", "glob", {"pattern": "**/*.txt"})],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
            for i in range(5)
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "loop forever"}],
        model="MiniMax-M2.7",
    ):
        events.append(ev)

    types = [ev.type for ev in events]
    assert types.count("assistant_message") == 5
    assert types.count("tool_result") == 5
    assert types.count("error") == 1
    assert types.count("done") == 1
    # error event mentions "max iterations"
    error_ev = next(ev for ev in events if ev.type == "error")
    assert "max iterations" in error_ev.content.lower()
    # done still last
    assert events[-1].type == "done"
    # Router was called exactly max_iterations times (no extra after the cap)
    assert router.call_count == 5


async def test_loop_emits_done_even_after_error(tmp_path: Path) -> None:
    """After an error event, done is still yielded so the client can finalise."""
    runtime = ToolRuntime(project_root=tmp_path)
    router = FakeRouter(
        scripted_responses=[
            CompletionResult(
                content="iter",
                tool_calls=[_make_tool_call("c1", "glob", {"pattern": "**/*.txt"})],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )
        ]
        * 5
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "loop"}], model="MiniMax-M2.7"
    ):
        events.append(ev)

    # Find the index of the first error
    error_idx = next(i for i, ev in enumerate(events) if ev.type == "error")
    # After the error there must be at least one more event (the done)
    assert any(ev.type == "done" for ev in events[error_idx + 1 :])
    # done is the very last event
    assert events[-1].type == "done"


async def test_loop_passes_tools_to_router(tmp_path: Path) -> None:
    """AgentLoop forwards TOOL_SCHEMAS to router.completion on every call."""
    runtime = ToolRuntime(project_root=tmp_path)
    router = FakeRouter(
        scripted_responses=[
            CompletionResult(content="ok", tool_calls=None, usage={}, cost=0.0),
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    async for _ in loop.run(
        messages=[{"role": "user", "content": "hi"}], model="MiniMax-M2.7"
    ):
        pass

    assert router.call_count == 1
    call = router.calls[0]
    # The router must have received TOOL_SCHEMAS, not None / []
    assert call["tools"] is TOOL_SCHEMAS
    # Model is forwarded
    assert call["model"] == "MiniMax-M2.7"
