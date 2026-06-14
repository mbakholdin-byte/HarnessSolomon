"""Agent loop — multi-iteration LLM ↔ tool execution (Шаг 6).

The loop alternates between:
  1. Calling the LLM (non-streaming, via ``LLMRouter.completion``).
  2. Emitting an ``assistant_message`` event with the LLM content.
  3. If the LLM requested tools, executing each through ``ToolRuntime``
     and emitting a ``tool_result`` event.
  4. Appending the LLM response and tool results back to the message
     history so the next iteration has full context.

The loop terminates when:
  * The LLM produces no tool calls (final answer reached), or
  * ``max_iterations`` is exceeded (we emit an ``error`` event and
    still close with ``done`` so the client can finalise).

A ``done`` event is always the last event yielded.

Streaming is intentionally NOT used here — ``completion()`` is sufficient
for Шаг 6. Шаг 7 (WebSocket chat) will switch to ``streaming_completion``
to forward tokens live.
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

    # --- public API ---

    async def run(
        self,
        messages: list[dict[str, Any]],
        model: str,
    ) -> AsyncIterator[StreamEvent]:
        """Drive the LLM ↔ tool loop and yield events.

        The input ``messages`` list is mutated in place: assistant and
        tool messages are appended as the loop progresses. The caller
        can pass an empty list (system prompt is added automatically)
        or a pre-built list (e.g. loaded from a session).

        Yields (in order, per iteration):
          * ``assistant_message`` — the LLM's content + usage + cost
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

        last_event: StreamEvent | None = None
        try:
            for _ in range(self.max_iterations):
                response = await self.router.completion(
                    messages=messages,
                    model=model,
                    tools=TOOL_SCHEMAS,
                )

                # 1. Assistant message event (always emitted, even if
                #    the model also requested tool calls).
                assistant_event = StreamEvent(
                    type="assistant_message",
                    content=response.content,
                    usage=dict(response.usage) if response.usage else None,
                    cost=response.cost,
                )
                yield assistant_event
                last_event = assistant_event

                # 2. Record the assistant turn in the message history.
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content,
                }
                if response.tool_calls:
                    # Persist the tool_calls exactly as the router gave
                    # them to us so the LLM can match tool_call_id in
                    # the next turn if needed.
                    assistant_msg["tool_calls"] = list(response.tool_calls)
                messages.append(assistant_msg)

                # 3. If there are no tool calls, the loop is done.
                if not response.tool_calls:
                    break

                # 4. Execute each tool call and emit a tool_result event.
                for tool_call in response.tool_calls:
                    name = (
                        (tool_call.get("function") or {}).get("name")
                        or tool_call.get("name")
                        or ""
                    )
                    args = _coerce_args(tool_call.get("function"))

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
