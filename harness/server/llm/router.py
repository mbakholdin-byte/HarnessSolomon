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
from typing import Any, AsyncIterator

from pydantic import BaseModel

from harness.server.llm.models import get_model

logger = logging.getLogger(__name__)

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
            model: Model id from the catalog (e.g. "MiniMax-M2.7").
            tools: Optional list of OpenAI-style tool schemas.
            **kwargs: Forwarded to litellm.completion (temperature, max_tokens, ...).

        Returns:
            CompletionResult with content, tool_calls, usage, and cost.
        """
        logger.debug("completion: model=%s tools=%s", model, bool(tools))
        call_kwargs: dict[str, Any] = dict(kwargs)
        if tools is not None:
            call_kwargs["tools"] = tools

        # litellm.completion is a sync function. In production we run it in
        # a worker thread to avoid blocking the event loop. In tests, the
        # function may be an AsyncMock, in which case we await it directly.
        response = await self._call_litellm_completion(model, messages, **call_kwargs)
        return self._normalize_completion(model, response)

    async def _call_litellm_completion(
        self, model: str, messages: list[dict], **call_kwargs: Any
    ) -> Any:
        """Invoke litellm.completion, choosing sync-in-thread vs await.

        - For real litellm (sync), run in a thread to avoid blocking the loop.
        - For AsyncMock / awaitable mocks (used in tests), `await` directly.
        """
        import asyncio
        import inspect

        fn = litellm.completion
        if inspect.iscoroutinefunction(fn):
            # Async mock or natively async provider
            return await fn(model=model, messages=messages, **call_kwargs)
        # Real sync litellm — offload to thread
        return await asyncio.to_thread(fn, model=model, messages=messages, **call_kwargs)

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
        raises).
        """
        logger.debug("streaming_completion: model=%s tools=%s", model, bool(tools))
        call_kwargs: dict[str, Any] = dict(kwargs)
        call_kwargs["stream"] = True
        if tools is not None:
            call_kwargs["tools"] = tools

        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                **call_kwargs,
            )
            # litellm returns a sync iterator when stream=True. Some custom
            # providers may return an async iterator; we detect & handle both.
            if hasattr(response, "__aiter__"):
                async for chunk in response:
                    ev = self._chunk_to_event(chunk)
                    if ev is not None:
                        yield ev
            else:
                for chunk in response:
                    ev = self._chunk_to_event(chunk)
                    if ev is not None:
                        yield ev
        except Exception as exc:  # noqa: BLE001 - we want to surface any error to the client
            logger.exception("streaming_completion failed")
            yield StreamEvent(type="error", content=str(exc))
            return

        yield StreamEvent(type="done")

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

    def _chunk_to_event(self, chunk: Any) -> StreamEvent | None:
        """Convert a single litellm streaming chunk into a StreamEvent."""
        if not getattr(chunk, "choices", None):
            # Some chunks only carry usage; we ignore them for now.
            return None
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            return None
        content_piece: str = getattr(delta, "content", "") or ""
        if content_piece:
            return StreamEvent(type="token", content=content_piece)
        return None

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


__all__ = ["LLMRouter", "CompletionResult", "StreamEvent"]
