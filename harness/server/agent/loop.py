"""Agent loop — multi-iteration LLM ↔ tool execution (Шаг 6, Phase 0+).

The loop alternates between:
  1. Calling the LLM (streaming via ``LLMRouter.streaming_completion``
     when ``stream=True`` — default — or non-streaming via
     ``LLMRouter.completion`` when ``stream=False``).
  2. Emitting ``token`` events live (streaming) or a single
     ``assistant_message`` event (non-streaming) with the LLM content.
  3. If the LLM requested tools, executing each through ``ToolRuntime``
     and emitting a ``tool_result`` event.
  4. Appending the LLM response and tool results back to the message
     history so the next iteration has full context.

The loop terminates when:
  * The LLM produces no tool calls (final answer reached), or
  * ``max_iterations`` is exceeded (we emit an ``error`` event and
    still close with ``done`` so the client can finalise).

A ``done`` event is always the last event yielded.

Streaming note: when ``stream=True`` the loop auto-falls back to
``completion()`` if the supplied router does not implement
``streaming_completion``. This keeps unit tests (FakeRouter without
streaming) compatible with production (real LLMRouter with streaming).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from harness.server.agent.prompts import build_system_prompt
from harness.server.agent.runtime import ToolRuntime
from harness.server.agent.tools import TOOL_SCHEMAS
from harness.server.llm.router import LLMRouter, StreamEvent

logger = logging.getLogger(__name__)


# === Defaults ===

DEFAULT_MAX_ITERATIONS = 5


# === Helpers ===

def _coerce_args(function_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Extract args from a tool_call's ``function`` payload.

    The LLM router emits tool_calls in OpenAI's nested shape:
        {"id": ..., "type": "function",
         "function": {"name": ..., "arguments": <json string>}}
    The runtime expects flat kwargs. We unwrap and ``json.loads`` the
    arguments string. If parsing fails we return an empty dict and let
    the runtime report the missing/invalid args.
    """
    if not function_payload:
        return {}
    raw = function_payload.get("arguments")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("tool_call arguments not valid JSON: %r", raw[:200])
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {}


def _format_tool_content(result_output: str, result_error: str) -> str:
    """Combine a ToolResult's output and error into one string for the LLM.

    The agent loop appends this as the ``content`` of the ``tool`` role
    message. The model sees both stdout-ish output and any error message
    in a single block.
    """
    if not result_error:
        return result_output
    if not result_output:
        return f"[error] {result_error}"
    return f"{result_output}\n[error] {result_error}"


def _coerce_args_from_router_tool_call(tc: dict[str, Any]) -> dict[str, Any]:
    """Coerce args from a router-shaped tool_call dict.

    The router emits tool_calls in two slightly different shapes:
      - streaming path:   {"id": ..., "type": "function", "function": {"name": ..., "arguments": <json string>}}
      - completion path:  same
    Some providers may put args at the top level. We unwrap robustly.
    """
    fn = tc.get("function")
    if fn:
        return _coerce_args(fn)
    return _coerce_args(tc)


def _extract_tool_calls_from_stream_done(done_event: StreamEvent) -> list[dict[str, Any]]:
    """Extract a list of tool_call dicts from a streaming-completion 'done' event.

    The streaming router encodes a single tool call on the final 'done'
    event as ``done_event.tool_call`` (a router-normalised dict). We
    return a one-element list to mirror the completion() path which
    always returns ``tool_calls: list``. If the event carries no
    tool_call, we return an empty list.
    """
    if done_event.tool_call:
        return [dict(done_event.tool_call)]
    return []


# === AgentLoop ===

class AgentLoop:
    """Stateless-ish LLM ↔ tool driver.

    The loop holds references to a ``ToolRuntime`` (for executing tools)
    and an ``LLMRouter`` (for talking to the model). It does not own
    session state — the caller passes the full ``messages`` list and is
    responsible for persisting it.

    The loop is safe to instantiate per-request. ``max_iterations``
    caps the number of LLM round-trips to prevent runaway tool chains.
    """

    def __init__(
        self,
        runtime: ToolRuntime,
        router: LLMRouter,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        if max_iterations < 1:
            raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
        self.runtime = runtime
        self.router = router
        self.max_iterations = max_iterations
        # Cache whether router supports streaming; checked once at __init__
        # so the per-iteration hot path doesn't pay the hasattr cost.
        self._router_supports_streaming = hasattr(router, "streaming_completion")

    # --- public API ---

    async def run(
        self,
        messages: list[dict[str, Any]],
        model: str,
        stream: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        """Drive the LLM ↔ tool loop and yield events.

        The input ``messages`` list is mutated in place: assistant and
        tool messages are appended as the loop progresses. The caller
        can pass an empty list (system prompt is added automatically)
        or a pre-built list (e.g. loaded from a session).

        Args:
            messages: OpenAI-style message list.
            model:    Model id from the catalog.
            stream:   When True (default) use ``router.streaming_completion``
                      and forward ``token`` events live to the client.
                      When False, use ``router.completion`` and emit a
                      single ``assistant_message`` per iteration (Шаг 6
                      behaviour, kept for back-compat). If the router
                      does not implement ``streaming_completion`` we
                      silently fall back to ``completion()`` regardless
                      of ``stream`` to keep tests compatible.

        Yields (in order, per iteration):
          * ``token`` events (stream=True only) — one per piece of model output
          * ``assistant_message`` — the full content + usage + cost (always)
          * one ``tool_result`` per executed tool (if any tool_calls)
          * on cap: one ``error`` event with content "max iterations reached"
        Always yields a final ``done`` event.
        """
        if not isinstance(messages, list):
            raise TypeError("messages must be a list of dicts")
        if not model or not isinstance(model, str):
            raise ValueError("model must be a non-empty string")

        # Prepend a system message on the first call. If the caller
        # already provided one we leave it alone.
        if not messages or messages[0].get("role") != "system":
            system_content = build_system_prompt(
                project_root=self.runtime.project_root,
                tools=list(TOOL_SCHEMAS),
            )
            messages.insert(
                0,
                {"role": "system", "content": system_content},
            )

        # Effective streaming mode: requested AND router supports it.
        # Falling back to completion() avoids AttributeError on test fakes.
        use_streaming = stream and self._router_supports_streaming

        last_event: StreamEvent | None = None
        try:
            for _ in range(self.max_iterations):
                # Buffer for token accumulation in streaming mode.
                streamed_content: str = ""
                streamed_tool_calls: list[dict[str, Any]] = []
                streamed_usage: dict | None = None
                streamed_cost: float = 0.0

                if use_streaming:
                    async for chunk in self.router.streaming_completion(
                        messages=messages,
                        model=model,
                        tools=TOOL_SCHEMAS,
                    ):
                        if chunk.type == "token":
                            streamed_content += chunk.content
                            yield chunk
                        elif chunk.type == "done":
                            # Final aggregator: may carry tool_call + usage
                            streamed_tool_calls = _extract_tool_calls_from_stream_done(
                                chunk
                            )
                            if chunk.usage is not None:
                                streamed_usage = dict(chunk.usage)
                            streamed_cost = float(chunk.cost or 0.0)
                        elif chunk.type == "error":
                            # Surface streaming error and abort the loop.
                            yield chunk
                            last_event = chunk
                            return
                        # Other event types (e.g. tool_call mid-stream) — ignore
                    # Synthesise a CompletionResult-like view for the rest of the loop.
                    content = streamed_content
                    tool_calls: list[dict[str, Any]] = streamed_tool_calls
                    usage = streamed_usage
                    cost = streamed_cost
                else:
                    response = await self.router.completion(
                        messages=messages,
                        model=model,
                        tools=TOOL_SCHEMAS,
                    )
                    content = response.content
                    tool_calls = response.tool_calls or []
                    usage = dict(response.usage) if response.usage else None
                    cost = response.cost

                # 1. Assistant message event (always emitted, even if
                #    the model also requested tool calls). This carries
                #    the FULL text so the persistence layer has a
                #    complete record regardless of how tokens arrived.
                assistant_event = StreamEvent(
                    type="assistant_message",
                    content=content,
                    usage=usage,
                    cost=cost,
                )
                yield assistant_event
                last_event = assistant_event

                # 2. Record the assistant turn in the message history.
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                }
                if tool_calls:
                    # Persist the tool_calls exactly as the router gave
                    # them to us so the LLM can match tool_call_id in
                    # the next turn if needed.
                    assistant_msg["tool_calls"] = [dict(tc) for tc in tool_calls]
                messages.append(assistant_msg)

                # 3. If there are no tool calls, the loop is done.
                if not tool_calls:
                    break

                # 4. Execute each tool call and emit a tool_result event.
                for tool_call in tool_calls:
                    fn = tool_call.get("function") or {}
                    name = (
                        fn.get("name")
                        or tool_call.get("name")
                        or ""
                    )
                    args = _coerce_args_from_router_tool_call(tool_call)

                    tool_result = await self.runtime.execute(name, args)
                    content = _format_tool_content(
                        tool_result.output, tool_result.error
                    )

                    result_event = StreamEvent(
                        type="tool_result",
                        content=content,
                        tool_call={
                            "id": tool_call.get("id"),
                            "name": name,
                            "args": args,
                            "ok": tool_result.ok,
                        },
                    )
                    yield result_event
                    last_event = result_event

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "name": name,
                            "content": content,
                        }
                    )
                # Continue to the next iteration — the LLM will see
                # the new tool results and decide what to do next.
            else:
                # for-else: completed all iterations without a clean exit.
                error_event = StreamEvent(
                    type="error",
                    content="max iterations reached",
                )
                yield error_event
                last_event = error_event
                logger.warning(
                    "AgentLoop hit max_iterations=%d for model=%s",
                    self.max_iterations,
                    model,
                )
        except Exception as exc:  # noqa: BLE001 — surface to the client
            logger.exception("AgentLoop.run failed")
            err_event = StreamEvent(type="error", content=f"{type(exc).__name__}: {exc}")
            yield err_event
            last_event = err_event

        # Always close with done so the client can finalise.
        _ = last_event  # silence linters; useful for future hooks
        yield StreamEvent(type="done")


__all__ = ["AgentLoop", "DEFAULT_MAX_ITERATIONS"]
