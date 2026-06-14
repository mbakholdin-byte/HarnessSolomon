"""Tests for AgentLoop (Шаг 6, Phase 0).

Per TDD (RED → GREEN → REFACTOR): tests first, then minimal implementation.

The AgentLoop is responsible for:
  * iterating between LLM completions and tool executions,
  * capping the number of iterations (default 5),
  * yielding a stream of events the WebSocket layer can forward.

The tests use a FakeRouter (programmed responses) and a real ToolRuntime
bound to a temp project_root. We do NOT mock the runtime — the safety
layer must remain in the loop.

Streaming mode (TODO from napkin.md, Phase 0+ cleanup):
  When ``AgentLoop.run(..., stream=True)`` is used, the loop calls
  ``router.streaming_completion()`` and yields token events live. The
  final ``assistant_message`` event still carries the full text + usage
  + cost for persistence.
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


# === Streaming mode (Phase 0+ — TODO from napkin.md) ===
#
# AgentLoop.run(stream=True) uses router.streaming_completion() instead of
# router.completion(). The user-facing API still yields:
#   - one or more "token" events live as the model generates
#   - one "assistant_message" at the end of the iteration carrying the
#     FULL text + usage + cost (for DB persistence)
#   - "tool_result" events for tool executions
#   - "done" as the very last event
#
# We do NOT change the contract for non-streaming callers (default
# stream=False preserves Шаг 6 behaviour exactly).


class StreamingFakeRouter:
    """Fake router that supports both completion() and streaming_completion().

    Each scripted turn is a tuple (content, tool_calls) — the router
    emits a sequence of StreamEvent "token" events, then a "done" with
    the aggregated content / tool_calls / usage.
    """

    def __init__(self, scripted_turns: list[dict[str, Any]]) -> None:
        self.scripted_turns = scripted_turns
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    async def completion(
        self, messages, model, tools=None, **kwargs
    ) -> CompletionResult:
        raise NotImplementedError("stream=True path must use streaming_completion")

    async def streaming_completion(
        self, messages, model, tools=None, **kwargs
    ):
        self.calls.append(
            {"messages": list(messages), "model": model, "tools": tools}
        )
        if self.call_count >= len(self.scripted_turns):
            raise RuntimeError("StreamingFakeRouter: out of scripted turns")
        turn = self.scripted_turns[self.call_count]
        self.call_count += 1
        # Emit one token event per character, then done.
        content = turn.get("content", "")
        for ch in content:
            yield StreamEvent(type="token", content=ch)
        yield StreamEvent(
            type="done",
            content=content,
            tool_call=turn.get("tool_call_dict"),
            usage=turn.get("usage", {}),
            cost=turn.get("cost", 0.0),
        )


async def test_loop_streaming_emits_token_events(tmp_path: Path) -> None:
    """stream=True: one token event per character + assistant_message at the end."""
    runtime = ToolRuntime(project_root=tmp_path)
    router = StreamingFakeRouter(
        scripted_turns=[
            {
                "content": "Hello, world!",
                "usage": {"prompt_tokens": 5, "completion_tokens": 13, "total_tokens": 18},
                "cost": 0.0001,
            }
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "hi"}],
        model="MiniMax-M2.7",
        stream=True,
    ):
        events.append(ev)

    types = [ev.type for ev in events]
    # We should have 13 token events (length of "Hello, world!") + 1 assistant + 1 done
    assert types.count("token") == 13
    assert types.count("assistant_message") == 1
    assert types.count("done") == 1
    assert events[-1].type == "done"

    # The token events concatenate to the full text
    token_text = "".join(ev.content for ev in events if ev.type == "token")
    assert token_text == "Hello, world!"

    # The final assistant_message carries the FULL content + usage + cost
    assistant_ev = next(ev for ev in events if ev.type == "assistant_message")
    assert assistant_ev.content == "Hello, world!"
    assert assistant_ev.usage is not None
    assert assistant_ev.usage["completion_tokens"] == 13
    assert assistant_ev.cost == 0.0001


async def test_loop_streaming_default_is_true(tmp_path: Path) -> None:
    """stream defaults to True (UI uses Web streaming) — but only when
    router supports streaming_completion. With FakeRouter (no streaming)
    the loop must fall back to completion() to keep backward compat.
    """
    runtime = ToolRuntime(project_root=tmp_path)
    # FakeRouter has only completion() — no streaming_completion
    router = FakeRouter(
        scripted_responses=[
            CompletionResult(content="ok", tool_calls=None, usage={}, cost=0.0)
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "hi"}],
        model="MiniMax-M2.7",
    ):
        events.append(ev)

    # If fallback works, we get the standard 1 assistant + 1 done
    types = [ev.type for ev in events]
    assert types.count("assistant_message") == 1
    assert types.count("done") == 1
    assert events[-1].type == "done"


async def test_loop_streaming_with_tool_call(tmp_path: Path) -> None:
    """stream=True with a tool_call: tokens → tool_result → next iteration tokens → done."""
    (tmp_path / "data.txt").write_text("answer = 42", encoding="utf-8")

    runtime = ToolRuntime(project_root=tmp_path)
    router = StreamingFakeRouter(
        scripted_turns=[
            # Iter 1: model decides to read the file
            {
                "content": "Reading...",
                "tool_call_dict": _make_tool_call(
                    "call_1", "read_file", {"path": "data.txt"}
                ),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            # Iter 2: model produces the final answer
            {
                "content": "The file says: 42",
                "usage": {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6},
                "cost": 0.0001,
            },
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "read data.txt"}],
        model="MiniMax-M2.7",
        stream=True,
    ):
        events.append(ev)

    types = [ev.type for ev in events]
    # Iter 1: 8 tokens ("Reading") + 1 assistant + 1 tool_result
    # Iter 2: 19 tokens ("The file says: 42") + 1 assistant + 1 done
    assert types.count("token") == 8 + 19
    assert types.count("assistant_message") == 2
    assert types.count("tool_result") == 1
    assert types.count("done") == 1
    assert events[-1].type == "done"

    # Tool result event carries the file content
    tool_result_ev = next(ev for ev in events if ev.type == "tool_result")
    assert "answer = 42" in tool_result_ev.content


async def test_loop_streaming_caps_at_max_iterations(tmp_path: Path) -> None:
    """stream=True still caps at max_iterations and emits error event."""
    runtime = ToolRuntime(project_root=tmp_path)
    # 5 turns, each with a tool call
    router = StreamingFakeRouter(
        scripted_turns=[
            {
                "content": f"i{i}",
                "tool_call_dict": _make_tool_call(
                    f"call_{i}", "glob", {"pattern": "**/*.txt"}
                ),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            for i in range(5)
        ]
    )
    loop = AgentLoop(runtime=runtime, router=router, max_iterations=5)

    events: list[StreamEvent] = []
    async for ev in loop.run(
        messages=[{"role": "user", "content": "loop forever"}],
        model="MiniMax-M2.7",
        stream=True,
    ):
        events.append(ev)

    types = [ev.type for ev in events]
    assert types.count("assistant_message") == 5
    assert types.count("tool_result") == 5
    assert types.count("error") == 1
    assert types.count("done") == 1
    error_ev = next(ev for ev in events if ev.type == "error")
    assert "max iterations" in error_ev.content.lower()
    assert events[-1].type == "done"
