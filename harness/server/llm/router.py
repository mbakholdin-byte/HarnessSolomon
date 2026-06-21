"""Solomon Harness — LiteLLM router.

Thin async wrapper around `litellm` exposing:
  - `LLMRouter.completion(...)`        — single-shot async completion
  - `LLMRouter.streaming_completion(...)` — async iterator over StreamEvent

The router is intentionally minimal: model catalog + provider routing is
already handled by litellm itself. We just normalize the result shape into
`CompletionResult` / `StreamEvent` so the rest of the harness can stay
provider-agnostic.

`cost` is computed from catalog pricing × token usage when litellm does not
return a `_hidden_params["response_cost"]` of its own.
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from pydantic import BaseModel

from harness.observability import emit_llm_call
from harness.observability.llm_usage_log import LlmUsageLogger
from harness.server.llm.models import DEFAULT_MAX_TOOLS, get_model

logger = logging.getLogger(__name__)

# === In-process metrics (Phase 0+; Prometheus comes in Phase 4) ===
#
#: Count of tool-truncation events per model, since process start.
#: Cheap dict for ``/api/metrics/truncations`` style endpoint (added
#: on demand, not pre-baked). Phase 4 will move to Prometheus with the
#: ``llm_tool_truncation_total{model=...}`` shape.
_truncation_counters: dict[str, int] = {}


def get_truncation_counts() -> dict[str, int]:
    """Return a snapshot of tool-truncation counts per model.

    Read-only copy — callers must not mutate the returned dict (the
    underlying counter is module-private and process-scoped).
    """
    return dict(_truncation_counters)


def reset_truncation_counts() -> None:
    """Zero out all truncation counters. Useful for tests."""
    _truncation_counters.clear()


# litellm is an optional-but-strongly-recommended dependency. We try to
# import it lazily so that the catalog + /api/models endpoint still work
# even on machines where the heavy litellm install is undesirable.
try:
    import litellm  # type: ignore[import-untyped]

    _LITELLM_AVAILABLE = True
except ImportError as _exc:  # pragma: no cover - exercised via test_router_handles_missing_litellm
    litellm = None  # type: ignore[assignment]
    _LITELLM_AVAILABLE = False
    _IMPORT_ERROR: Exception | None = _exc
else:
    _IMPORT_ERROR = None


# === Schemas ===

class CompletionResult(BaseModel):
    """Normalized completion result."""

    content: str
    tool_calls: list[dict] | None = None
    usage: dict = {}            # prompt_tokens / completion_tokens / total_tokens
    cost: float = 0.0


class StreamEvent(BaseModel):
    """One chunk of a streaming completion."""

    type: str                   # "token" | "tool_call" | "done" | "error"
    content: str = ""
    tool_call: dict | None = None
    usage: dict | None = None
    cost: float | None = None


# === Router ===

class LLMRouter:
    """Async wrapper around litellm.

    Stateless: all per-call config (api_key, base_url, etc.) is taken from
    the env at call-time by litellm itself.
    """

    def __init__(self) -> None:
        if not _LITELLM_AVAILABLE:
            raise RuntimeError(
                "litellm is required for LLMRouter. "
                "Install litellm>=1.40 (already declared in pyproject.toml). "
                f"Original error: {_IMPORT_ERROR}"
            )
        self._usage_logger: LlmUsageLogger | None = None

    def set_usage_logger(self, logger: LlmUsageLogger) -> None:
        """Wire an NDJSON usage logger for Phase 7.6 calibration tracking.

        Called at server startup (lifespan) after settings are loaded.
        """
        self._usage_logger = logger

    # --- non-streaming ---

    async def completion(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        """Run a single async completion.

        Args:
            messages: OpenAI-style message list.
            model: Model id from the catalog (e.g. "MiniMax-M2.7"). The router
                maps it to the litellm form ("minimax/MiniMax-M2.7") via
                `_to_litellm_model_id`. You can also pass an already-prefixed
                id (e.g. "openai/gpt-4o") to bypass catalog lookup.
            tools: Optional list of OpenAI-style tool schemas.
            **kwargs: Forwarded to litellm.completion (temperature, max_tokens, ...).

        Returns:
            CompletionResult with content, tool_calls, usage, and cost.
        """
        logger.debug("completion: model=%s tools=%s", model, bool(tools))
        # Phase 3 v1.4.0: Anthropic prompt caching (router-level
        # cache_control injection). No-op when disabled or for
        # non-Anthropic models. See ``_maybe_inject_cache_control``.
        messages = self._maybe_inject_cache_control(messages, model)
        call_kwargs: dict[str, Any] = dict(kwargs)
        if tools is not None:
            call_kwargs["tools"] = tools

        # Phase 4.1 Step 6.3: observe LLM call (latency, cost, tokens).
        tier = "T3"  # default
        try:
            spec = get_model(model)
            if spec is not None:
                tier = spec.tier
        except Exception:  # noqa: BLE001 — never block on tier lookup
            pass
        start = time.monotonic()
        status = "ok"
        error_msg = ""
        try:
            # litellm.completion is a sync function. In production we run it in
            # a worker thread to avoid blocking the event loop. In tests, the
            # function may be an AsyncMock, in which case we await it directly.
            response = await self._call_litellm_completion(model, messages, **call_kwargs)
        except Exception as exc:  # noqa: BLE001 — capture for observability
            status = "error"
            error_msg = str(exc)
            duration = time.monotonic() - start
            emit_llm_call(
                model=model,
                tier=tier,
                prompt_tokens=0,
                completion_tokens=0,
                duration_s=duration,
                status=status,
                error=error_msg,
                # Phase 4.9 v1.19.0: emit the per-model breakdown so
                # error paths also surface in dashboards (a model
                # that only errors will show tokens=0 but a non-zero
                # call count, which is the signal operators want).
                model_id=model,
                cost_usd_override=0.0,
            )
            raise
        result = self._normalize_completion(model, response)
        duration = time.monotonic() - start
        try:
            usage = result.usage or {}
            emit_llm_call(
                model=model,
                tier=tier,
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                duration_s=duration,
                status=status,
                # Phase 4.9 v1.19.0: pass the cost already computed by
                # ``_normalize_completion`` (which uses the catalog
                # pricing table) so the breakdown counters see the
                # same value as ``CompletionResult.cost``. ``model_id``
                # is the catalog id (same as ``model`` here).
                model_id=model,
                cost_usd_override=result.cost,
            )
            # Phase 7.6: NDJSON usage log for calibration
            if self._usage_logger:
                self._usage_logger.log_usage({
                    "event": "llm_completion",
                    "model": model,
                    "tier": tier,
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "total_tokens": int(usage.get("total_tokens", 0) or 0),
                    "cost_usd": result.cost,
                    "duration_s": duration,
                    "status": status,
                })
        except Exception:  # noqa: BLE001 — observability must never break completion
            logger.debug("emit_llm_call failed", exc_info=True)
        return result

    async def _call_litellm_completion(
        self, model: str, messages: list[dict], **call_kwargs: Any
    ) -> Any:
        """Invoke litellm.completion, choosing sync-in-thread vs await.

        - For real litellm (sync), run in a thread to avoid blocking the loop.
        - For AsyncMock / awaitable mocks (used in tests), `await` directly.

        Maps the catalog id (e.g. "MiniMax-M2.7") to its litellm-compatible
        form ("minimax/MiniMax-M2.7") by looking up the model spec and
        prefixing the provider. If the model is already prefixed (contains
        "/") or unknown to the catalog, the original id is passed through.
        """
        import asyncio
        import inspect

        litellm_model = self._to_litellm_model_id(model)
        # Apply per-model tool limit (e.g. MiniMax rejects >4 with code 2013)
        if "tools" in call_kwargs:
            call_kwargs["tools"] = self._limit_tools_for_model(
                model, call_kwargs["tools"]
            )
        # Normalize tool schemas to OpenAI's wrapped form
        # (litellm's minimax provider doesn't auto-wrap; MiniMax API
        # rejects unwrapped tools with "invalid tool type:").
        if "tools" in call_kwargs:
            call_kwargs["tools"] = self._wrap_tools_for_litellm(call_kwargs["tools"])
        fn = litellm.completion
        if inspect.iscoroutinefunction(fn):
            # Async mock or natively async provider
            return await fn(model=litellm_model, messages=messages, **call_kwargs)
        # Real sync litellm — offload to thread
        return await asyncio.to_thread(
            fn, model=litellm_model, messages=messages, **call_kwargs
        )

    @staticmethod
    def _to_litellm_model_id(model: str) -> str:
        """Map catalog id to litellm-compatible id with provider prefix.

        Catalog ids like "MiniMax-M2.7" are user-facing. litellm requires
        "{provider}/{model}" form (e.g. "minimax/MiniMax-M2.7"). This helper
        looks up the catalog and prefixes the provider. Pass-through if the
        id is already prefixed (contains "/") or unknown.
        """
        if "/" in model:
            # Already in provider/model form — assume caller knows what they want
            return model
        spec = get_model(model)
        if spec is None:
            # Unknown model — let litellm produce its own error so the
            # caller sees the original message
            return model
        return f"{spec.provider}/{spec.id}"

    @staticmethod
    def _limit_tools_for_model(
        model: str, tools: list[dict] | None
    ) -> list[dict] | None:
        """Cap the number of tools sent to the model at its per-spec limit.

        Different providers have different per-request tool limits.
        MiniMax's effective limit is at least 32 (verified 2026-06-14
        with live calls). We use the per-spec cap, or
        ``models.DEFAULT_MAX_TOOLS`` (16) as a safe default for unknown
        models.

        On truncation: emits a structured warning (with dropped tool
        names) AND increments the ``llm_tool_truncation_total`` counter
        for the model. The counter is in-memory per process — good
        enough for Phase 0 observability. Phase 4 will move this to
        Prometheus.
        """
        if not tools:
            return tools
        spec = get_model(model)
        max_tools = spec.max_tools if spec is not None else DEFAULT_MAX_TOOLS
        if len(tools) <= max_tools:
            return tools
        truncated = tools[:max_tools]
        dropped = [
            t.get("name", "?") if isinstance(t, dict) else "?"
            for t in tools[max_tools:]
        ]
        logger.warning(
            "tools truncated for model=%s: kept %d of %d, model max=%d. "
            "Dropped tools: %s",
            model,
            max_tools,
            len(tools),
            max_tools,
            dropped,
        )
        # Increment the in-process counter for the model. Phase 4
        # (observability) will swap this for a Prometheus metric.
        _truncation_counters[model] = _truncation_counters.get(model, 0) + 1
        return truncated

    @staticmethod
    def _wrap_tools_for_litellm(tools: list[dict] | None) -> list[dict] | None:
        """Wrap tool schemas in OpenAI's ``{"type": "function", ...}`` form.

        litellm's built-in providers (openai, anthropic) auto-wrap tool
        schemas. The ``minimax`` provider does not, so unwrapped schemas
        reach the wire as ``{"name": ..., "description": ..., "parameters": ...}``
        and MiniMax's API rejects them with ``invalid tool type: `` (code 2013).

        Pass-through for tools already in the wrapped form (have ``type: function``)
        and for tools that are not dicts (defensive).
        """
        if not tools:
            return tools
        wrapped: list[dict] = []
        for tool in tools:
            if not isinstance(tool, dict):
                wrapped.append(tool)
                continue
            if tool.get("type") == "function" and "function" in tool:
                # Already in OpenAI wrapped form
                wrapped.append(tool)
                continue
            # Unwrapped form: {"name", "description", "parameters"}
            if "name" in tool and "parameters" in tool:
                wrapped.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool["parameters"],
                        },
                    }
                )
            else:
                # Unknown shape — pass through and let litellm error
                wrapped.append(tool)
        return wrapped

    # --- streaming ---

    async def streaming_completion(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a completion, yielding StreamEvent chunks.

        Always ends with a `done` event (or `error` if the underlying stream
        raises). The final ``done`` event carries the aggregated
        ``content``, ``tool_calls`` (a list of router-normalised dicts —
        usually 0 or 1 element for cloud models), ``usage`` and ``cost``
        so callers can persist a complete record.
        """
        logger.debug("streaming_completion: model=%s tools=%s", model, bool(tools))
        # Phase 3 v1.4.0: Anthropic prompt caching (router-level
        # cache_control injection). No-op when disabled or for
        # non-Anthropic models.
        messages = self._maybe_inject_cache_control(messages, model)
        call_kwargs: dict[str, Any] = dict(kwargs)
        call_kwargs["stream"] = True
        if tools is not None:
            call_kwargs["tools"] = tools

        litellm_model = self._to_litellm_model_id(model)
        if "tools" in call_kwargs:
            call_kwargs["tools"] = self._limit_tools_for_model(
                model, call_kwargs["tools"]
            )
            call_kwargs["tools"] = self._wrap_tools_for_litellm(call_kwargs["tools"])
        # Phase 4.1 Step 6.3: tier lookup (same pattern as completion())
        tier = "T3"
        try:
            spec = get_model(model)
            if spec is not None:
                tier = spec.tier
        except Exception:  # noqa: BLE001
            pass
        start = time.monotonic()
        # Aggregator state for the final 'done' event
        content_buf: list[str] = []
        # tool_calls_buf: list of (index, dict) for delta accumulation
        tool_calls_buf: dict[int, dict[str, Any]] = {}
        usage_final: dict | None = None
        try:
            response = litellm.completion(
                model=litellm_model,
                messages=messages,
                **call_kwargs,
            )
            # Always drain via SYNC iteration in a worker thread.
            #
            # Why sync and not async? litellm wraps generator responses
            # in BaseModelResponseIterator which exposes BOTH __iter__
            # and __aiter__. The async path calls
            # self.streaming_response.__aiter__() — but if
            # streaming_response is a generator, that raises
            # `AttributeError: 'generator' object has no '__aiter__'`.
            # The sync path always works. The cost is one thread hop,
            # which is fine for streaming (we don't block the event
            # loop for the duration of the response).
            import asyncio

            chunks_sync = await asyncio.to_thread(lambda: list(response))
            for chunk in chunks_sync:
                token_ev, partial = self._chunk_to_event(
                    chunk, content_buf, tool_calls_buf
                )
                if token_ev is not None:
                    yield token_ev
                if partial.usage is not None:
                    usage_final = partial.usage
        except Exception as exc:  # noqa: BLE001 - we want to surface any error to the client
            logger.exception("streaming_completion failed")
            yield StreamEvent(type="error", content=str(exc))
            return

        # Final tool calls list, sorted by index
        tool_calls_final: list[dict[str, Any]] | None = None
        if tool_calls_buf:
            tool_calls_final = [tool_calls_buf[i] for i in sorted(tool_calls_buf)]
        cost = self._compute_cost(model, usage_final) if usage_final else 0.0
        duration = time.monotonic() - start
        # Phase 7.6: NDJSON usage log for calibration (streaming path)
        try:
            if self._usage_logger and usage_final:
                self._usage_logger.log_usage({
                    "event": "llm_completion",
                    "model": model,
                    "tier": tier,
                    "prompt_tokens": int(usage_final.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage_final.get("completion_tokens", 0) or 0),
                    "total_tokens": int(usage_final.get("total_tokens", 0) or 0),
                    "cost_usd": cost,
                    "duration_s": duration,
                    "status": "ok",
                })
        except Exception:  # noqa: BLE001 — observability must never break streaming
            logger.debug("usage log (streaming) failed", exc_info=True)
        yield StreamEvent(
            type="done",
            content="".join(content_buf),
            tool_call=tool_calls_final[0] if tool_calls_final else None,
            usage=usage_final,
            cost=cost,
        )

    # --- helpers ---

    def _normalize_completion(self, model: str, response: Any) -> CompletionResult:
        """Convert a litellm.ModelResponse into a CompletionResult."""
        choice = response.choices[0]
        message = choice.message
        content: str = getattr(message, "content", "") or ""
        tool_calls_raw = getattr(message, "tool_calls", None)
        tool_calls: list[dict] | None = None
        if tool_calls_raw:
            tool_calls = []
            for tc in tool_calls_raw:
                tool_calls.append(
                    {
                        "id": getattr(tc, "id", None),
                        "type": getattr(tc, "type", "function"),
                        "function": {
                            "name": getattr(tc.function, "name", None),
                            "arguments": getattr(tc.function, "arguments", None),
                        }
                        if getattr(tc, "function", None)
                        else None,
                    }
                )

        usage_obj = getattr(response, "usage", None)
        usage: dict[str, int] = {}
        if usage_obj is not None:
            usage = {
                "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
            }

        cost = self._compute_cost(model, usage) if usage else 0.0
        return CompletionResult(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            cost=cost,
        )

    def _chunk_to_event(
        self,
        chunk: Any,
        content_buf: list[str] | None = None,
        tool_calls_buf: dict[int, dict[str, Any]] | None = None,
    ) -> tuple[StreamEvent | None, "_StreamingPartial"]:
        """Convert a single litellm streaming chunk into a StreamEvent.

        The streaming router accumulates content + tool_call deltas in
        caller-provided buffers. Each tool_call may arrive across many
        chunks (id once, name once, arguments in pieces). We stitch
        them together by the delta's ``index`` so the final 'done'
        event carries a complete tool_call.

        Returns (event_to_yield, partial). The event is a ``token``
        event if the chunk carried new content; otherwise None. The
        ``partial`` carries any partial data extracted from this chunk
        (currently only ``usage`` when the model reports it mid-stream).
        """
        partial = _StreamingPartial()
        if not getattr(chunk, "choices", None):
            # Some chunks only carry usage (and no choices) — pick it up.
            u = getattr(chunk, "usage", None)
            if u is not None:
                partial.usage = {
                    "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                    "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
                }
            return None, partial
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            return None, partial

        content_piece: str = getattr(delta, "content", "") or ""
        if content_piece and content_buf is not None:
            content_buf.append(content_piece)

        # Tool-call deltas (OpenAI streaming shape)
        if tool_calls_buf is not None:
            for tc_delta in getattr(delta, "tool_calls", None) or []:
                idx = int(getattr(tc_delta, "index", 0) or 0)
                slot = tool_calls_buf.setdefault(
                    idx,
                    {
                        "id": None,
                        "type": "function",
                        "function": {"name": None, "arguments": ""},
                    },
                )
                if getattr(tc_delta, "id", None):
                    slot["id"] = tc_delta.id
                if getattr(tc_delta, "type", None):
                    slot["type"] = tc_delta.type
                fn = getattr(tc_delta, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["function"]["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["function"]["arguments"] += fn.arguments

        if content_piece:
            return StreamEvent(type="token", content=content_piece), partial
        return None, partial

    def _compute_cost(self, model: str, usage: dict[str, int]) -> float:
        """Compute cost in USD from catalog pricing × token usage.

        Falls back to 0.0 if the model is not in the catalog.
        """
        spec = get_model(model)
        if spec is None:
            return 0.0
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        # pricing is per 1M tokens
        cost = (prompt / 1_000_000.0) * spec.pricing_input + (
            completion / 1_000_000.0
        ) * spec.pricing_output
        return round(cost, 9)

    # --- Prompt caching (Phase 3 v1.4.0) ---

    def _maybe_inject_cache_control(
        self,
        messages: list[dict[str, Any]],
        model: str,
    ) -> list[dict[str, Any]]:
        """Return messages with Anthropic ``cache_control`` markers.

        Phase 3 v1.4.0 "Prompt caching" strategy (Anthropic 4-strategy
        playbook). When ``settings.prompt_cache_enabled`` is ``True``
        AND ``settings.prompt_cache_strategy == "anthropic"`` AND the
        model id starts with ``"anthropic/"`` we mark:

        * the first message (typically the system prompt) with
          ``{"type": "ephemeral"}``
        * the last two messages (latest user turn + trailing assistant
          context) with the same marker

        Anything else (strategy ``"vllm"``, ``"off"``, non-Anthropic
        model, disabled setting) → returns the input unchanged. The
        ``vllm`` strategy is a no-op here because vLLM prefix caching
        is an engine-level feature the operator configures outside
        the harness.

        Why inject at the router level (not the provider level)? The
        plan agent review (Phase 3 v1.4.0) flagged that adding an
        Anthropic provider module is out of scope for the 12-week
        roadmap. The router is the only place that already knows
        about the model id, and it forwards the message list as-is to
        litellm. We mutate a *copy* of the list / message dicts so
        callers are not surprised by side effects.

        Args:
            messages: The OpenAI-style message list.
            model: The model id passed to ``completion()`` or
                ``streaming_completion()``. Compared with
                ``startswith("anthropic/")`` after the
                ``_to_litellm_model_id`` pass — we accept both
                catalog and pre-prefixed ids.

        Returns:
            A new list with the markers injected, or the original
            list if the strategy is not active.
        """
        # Read settings defensively — the import is local to keep
        # the router importable in tests where settings is patched
        # before instantiation.
        try:
            from harness.config import settings as _settings
        except Exception:  # noqa: BLE001 — settings unavailable
            return messages

        if not getattr(_settings, "prompt_cache_enabled", False):
            return messages
        if getattr(_settings, "prompt_cache_strategy", "off") != "anthropic":
            return messages
        # Accept both catalog ids (e.g. "MiniMax-M2.7") and pre-prefixed
        # litellm ids (e.g. "anthropic/claude-sonnet-4-6").
        catalog_id = model
        if "/" not in catalog_id:
            # Map to litellm form so we can compare prefixes.
            try:
                catalog_id = self._to_litellm_model_id(model)
            except Exception:  # noqa: BLE001 — unknown model id
                return messages
        if not catalog_id.startswith("anthropic/"):
            return messages
        if not messages:
            return messages

        cache_control = {"type": "ephemeral"}
        # Shallow-copy each message dict so we can mutate without
        # surprising the caller. We also need a new list because we
        # only mark specific indices.
        out: list[dict[str, Any]] = []
        last_idx = len(messages) - 1
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                out.append(msg)
                continue
            new_msg = dict(msg)
            if i == 0 or i == last_idx or i == last_idx - 1:
                new_msg["cache_control"] = cache_control
            out.append(new_msg)
        return out


__all__ = ["LLMRouter", "CompletionResult", "StreamEvent", "get_truncation_counts", "reset_truncation_counts"]


# === Streaming helpers (private) ===

class _StreamingPartial:
    """Holds data extracted from a single streaming chunk that doesn't
    warrant its own yielded event (currently: usage info carried in
    some chunks without a content delta).
    """

    __slots__ = ("usage",)

    def __init__(self) -> None:
        self.usage: dict | None = None
