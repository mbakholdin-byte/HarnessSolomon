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

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from harness.server.agent.prompts import build_system_prompt
from harness.server.agent.runtime import ToolRuntime
from harness.server.agent.tools import TOOL_SCHEMAS
from harness.server.llm.router import LLMRouter, StreamEvent

if TYPE_CHECKING:
    from harness.context.compaction import ContextCompactor
from harness.redaction import redact_dict
# Phase 4.4+ v1.14.0: Stop hook fires at AgentLoop exit.
# Lazy import — keep the existing trust boundary: harness.hooks.*
# is stdlib-only, importing it eagerly is safe (it doesn't import
# harness.agents or harness.server).
from harness.hooks.runner import safe_fire

logger = logging.getLogger(__name__)


# === Defaults ===

DEFAULT_MAX_ITERATIONS = 5

# === Regex (Phase 3 v1.4.0) ===

#: Matches the ``id=N`` fragment in an offload stub produced by
#: ``ToolOffloader.build_stub``. We use this in
#: :meth:`AgentLoop._extract_offloaded_note_id` so reflection lessons
#: can reference the L2 storage by note id.
_OFFLOADED_ID_RE = re.compile(r"\bid=(\d+)\b")


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
        compactor: "ContextCompactor | None" = None,
    ) -> None:
        if max_iterations < 1:
            raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
        self.runtime = runtime
        self.router = router
        self.max_iterations = max_iterations
        # Phase 3: optional compactor. Default None → no-op (the loop
        # runs without context-size management, matching the pre-Phase-3
        # contract). The compactor is injected by ``server.app.lifespan``
        # when ``settings.compaction_enabled`` is True.
        self.compactor = compactor
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
            # Phase 3 v1.2.1: defence-in-depth L0 injection. When the
            # caller did NOT supply a system message (e.g. WebSocket
            # / CLI callers that build ``AgentLoop`` directly without
            # going through ``AgentRunner``), we apply the L0 section
            # stored on the runtime (``runner._drive`` sets it from
            # ``store.read_notes("L0", ...)``). The runner-built
            # ``messages`` list goes through the ``else`` branch below
            # — its first message is already a system prompt with the
            # L0 block prepended, and we don't touch it.
            l0_section = getattr(self.runtime, "_l0_section", None)
            if l0_section:
                system_content = f"{l0_section}\n\n{system_content}"
            messages.insert(
                0,
                {"role": "system", "content": system_content},
            )

        # Phase 3: compact the message list before the first LLM call
        # if it exceeds the configured threshold. The compactor returns
        # a NEW list; we rebind so the in-place ``messages.append``
        # below still works against the compacted set.
        # Phase 3 v1.5.0: pass ``force_idle_check=True`` so the
        # time/turn/hybrid trigger can fire BEFORE the token threshold
        # (Plan agent BLOCKER B8 — AgentLoop is the "active session"
        # path; ``Session.load_history`` is the "resume" path and
        # passes ``force_idle_check=False`` by default).
        if self.compactor is not None:
            messages = await self.compactor.maybe_compact(
                messages, model, force_idle_check=True,
            )
        # Phase 3: redact any PII / secrets in the message list before
        # the LLM call. ``redact_dict`` is a no-op for non-string
        # content and idempotent (running twice yields the same
        # result). The redacted content preserves structure so the
        # LLM can still reason about categories (``<EMAIL>``,
        # ``<GITHUB_TOKEN>``, etc.).
        from harness.config import settings as _settings
        if _settings.redaction_enabled:
            messages = redact_dict(messages, {"content"})  # type: ignore[assignment]

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
                # Phase 3 v1.4.0: record the assistant turn for
                # end-of-session reflection. The collected events are
                # consumed by ``SessionLifecycle.__aexit__`` which
                # passes them to ``ReflectionLoop.reflect``.
                self._record_event(kind="assistant", content=content)

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
                    # Phase 3 v1.3.1: offload large tool results to L2.
                    # When the formatted content exceeds the configured
                    # threshold (default 25 KB) we persist it to scratchpad
                    # L2 and replace ``content`` with a small stub so the
                    # chat budget isn't blown by a single tool output.
                    # The LLM can pull the full body via
                    # ``scratchpad_read_offloaded(id=N)`` or search across
                    # offloaded content via ``scratchpad_search_offloaded(query)``.
                    content = await self._maybe_offload_tool_result(
                        content=content,
                        name=name,
                        tool_call_id=tool_call.get("id"),
                    )
                    # Phase 3 v1.4.0: record this tool turn for reflection.
                    # The offloader returned either the original content
                    # (offload disabled / failed) or a stub. We record
                    # the *stored* content (stub when offloaded) so the
                    # lesson extractor sees the pointer, not the body.
                    offloaded_id = self._extract_offloaded_note_id(
                        content=content, original_full=tool_result.output,
                    )
                    self._record_event(
                        kind="tool",
                        content=content,
                        tool_name=name,
                        offloaded_id=offloaded_id,
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
        # Phase 4.4+ v1.14.0: Stop hook. Best-effort — the loop has
        # already exited, so ``block`` is logged-only (we cannot
        # un-yield events that have already been sent to the
        # client). Per docs/hooks.md, payload = reason, final_message,
        # iterations; we add agent_id as a sibling.
        try:
            _sid = getattr(self.runtime, "_session_id", "") or ""
            _aid = getattr(self.runtime, "_agent_id", "") or ""
        except Exception:  # noqa: BLE001
            _sid, _aid = "", ""
        await safe_fire(
            "Stop",
            session_id=_sid,
            agent_id=_aid,
            payload={
                "reason": "completed" if last_event is None or last_event.type != "error"
                          else "error",
                "final_message": (last_event.content[:200] if last_event and last_event.content else ""),
                "iterations": 0,  # AgentLoop doesn't track iteration count
                "agent_id": _aid,
            },
        )
        yield StreamEvent(type="done")

    # --- internal helpers ---

    async def _maybe_offload_tool_result(
        self,
        *,
        content: str,
        name: str,
        tool_call_id: str | None,
    ) -> str:
        """Offload a tool result to L2 if it exceeds the threshold.

        Returns the original ``content`` unchanged when the offload is
        disabled, fails, or the content is below the threshold. The
        caller (``run``) treats the return value as opaque and
        appends it directly to the message history.

        The offloader is read from the runtime via ``getattr`` so
        the loop can be constructed in tests without the offloader
        module being importable (mirror :attr:`ToolRuntime._l0_section`
        defence-in-depth from Phase 3 v1.2.1).

        The per-call timeout is honoured by the offloader's
        ``offload()`` call — we wrap it in ``asyncio.wait_for`` so a
        slow / hung SQLite write does not stall the chat loop.
        """
        offloader = getattr(self.runtime, "_tool_offloader", None)
        if offloader is None:
            return content
        if not offloader.should_offload(content):
            return content
        # Resolve the session id from the offloader's inner scratchpad
        # (mirror ``runtime.py:_scratchpad_l2_search`` getattr chain
        # from Phase 3 v1.3.0). When the offloader is constructed in
        # a test without a scratchpad, we fall back to "unknown".
        inner_scratchpad = getattr(offloader, "_scratchpad", None)
        session_id = getattr(inner_scratchpad, "_session_id", None) or "unknown"
        # Read the per-call timeout from the offloader's settings.
        # Using ``getattr`` for safety in case the offloader's
        # settings object doesn't carry the field (e.g. a custom
        # test double).
        settings = getattr(offloader, "_settings", None)
        timeout_ms = getattr(settings, "tool_offload_max_ms", 2000) or 2000
        timeout_s = max(0.05, float(timeout_ms) / 1000.0)
        try:
            note_id = await asyncio.wait_for(
                offloader.offload(
                    content,
                    tool_name=name,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "tool offload timeout (%.1fs) for tool=%s — keeping full content",
                timeout_s, name,
            )
            return content
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "tool offload failed for tool=%s: %s — keeping full content",
                name, exc,
            )
            return content
        if note_id is None:
            return content
        # Replace full content with stub. The offloader's build_stub
        # method owns the stub format (header + preview + read hint).
        return offloader.build_stub(
            content, note_id=note_id, tool_name=name,
        )

    # --- Phase 3 v1.4.0: SessionEvent collection for reflection ---

    def _extract_offloaded_note_id(
        self,
        *,
        content: str,
        original_full: str,
    ) -> int | None:
        """Return the L2 note id if ``content`` is an offload stub.

        The offloader builds a stub like::

            (offloaded 27123 bytes; id=42; tool=bash; read via
            scratchpad_read_offloaded(id=42))

        We detect the stub by comparing byte length — if the
        ``content`` we ended up with is much shorter than the
        ``original_full`` output, an offload must have happened. We
        then extract the integer ``id=N`` with a regex.

        Returns ``None`` when no offload happened (full content kept
        inline) or when the stub format is unrecognised.
        """
        if not isinstance(content, str) or not isinstance(original_full, str):
            return None
        # Quick reject: offload only fires when the result is large.
        if len(content) >= len(original_full):
            return None
        m = _OFFLOADED_ID_RE.search(content)
        if m is None:
            return None
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            return None

    def _record_event(
        self,
        *,
        kind: str,
        content: str,
        tool_name: str | None = None,
        offloaded_id: int | None = None,
    ) -> None:
        """Append a ``SessionEvent`` to the runtime's collector.

        Phase 3 v1.4.0: the collector is a plain list wired into
        ``ToolRuntime`` at construction. ``SessionLifecycle.__aexit__``
        reads it (via ``getattr(runtime, "_events_collector", None)``)
        and passes it to ``ReflectionLoop.reflect``.

        We construct ``SessionEvent`` instances defensively:
        * If the loop has been imported without the reflection module
          (test path), we fall back to a duck-typed dict-like object
          with the same field names. Reflection / lifecycle are the
          only consumers of this list, and both use ``getattr`` /
          dataclass field access.
        * Collector being ``None`` is a no-op (backward compat with
          v1.3.x runtimes that did not collect events).

        The function is intentionally best-effort — losing an event
        is not a failure mode the user should ever see. We log and
        swallow any error (e.g. collector is a frozen tuple).
        """
        collector = getattr(self.runtime, "_events_collector", None)
        if collector is None:
            return
        # Build a SessionEvent when possible, fall back to a simple
        # namespace-like dict for test paths.
        try:
            from harness.server.agent.reflection_loop import SessionEvent
            event = SessionEvent(
                kind=kind,  # type: ignore[arg-type]
                content=content,
                ts=time.time(),
                tool_name=tool_name,
                offloaded_id=offloaded_id,
            )
        except Exception:  # noqa: BLE001 — module unavailable, fall back
            event = {
                "kind": kind,
                "content": content,
                "ts": time.time(),
                "tool_name": tool_name,
                "offloaded_id": offloaded_id,
            }
        try:
            append = getattr(collector, "append", None)
            if append is None:
                return
            append(event)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "AgentLoop: failed to record %s event: %s", kind, exc,
            )


__all__ = ["AgentLoop", "DEFAULT_MAX_ITERATIONS"]
