"""Tests for LLMRouter (Шаг 5) — wrapped with unittest.mock to avoid real API calls.

Per TDD (RED → GREEN → REFACTOR): tests first, then minimal implementation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.server.llm.router import LLMRouter


def _make_completion_response(
    content: str = "Hello!",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> MagicMock:
    """Build a mock litellm completion response."""
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    choice.finish_reason = "stop"

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    response.model = "MiniMax-M2.7"
    return response


def _make_stream_chunks(content: str = "Hi") -> list[MagicMock]:
    """Build mock streaming chunks."""
    chunks: list[MagicMock] = []
    for i, ch in enumerate(content):
        delta = MagicMock()
        delta.content = ch
        delta.tool_calls = None
        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = None if i < len(content) - 1 else "stop"
        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = None
        chunks.append(chunk)
    return chunks


# === completion() ===

async def test_completion_returns_result() -> None:
    """router.completion returns CompletionResult with content + usage."""
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        mock_litellm.completion = AsyncMock(
            return_value=_make_completion_response("Hello, world!", 12, 4)
        )
        router = LLMRouter()
        result = await router.completion(
            messages=[{"role": "user", "content": "Hi"}],
            model="MiniMax-M2.7",
        )
        assert result.content == "Hello, world!"
        assert result.usage["prompt_tokens"] == 12
        assert result.usage["completion_tokens"] == 4
        assert result.usage["total_tokens"] == 16
        assert result.cost >= 0.0


async def test_completion_passes_tools() -> None:
    """completion() forwards tools to litellm."""
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        mock_litellm.completion = AsyncMock(
            return_value=_make_completion_response("ok", 1, 1)
        )
        router = LLMRouter()
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        await router.completion(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
            tools=tools,
        )
        # Verify tools were passed; model is mapped to provider-prefixed form
        call_kwargs = mock_litellm.completion.call_args.kwargs
        assert call_kwargs.get("tools") == tools
        assert call_kwargs.get("model") == "minimax/MiniMax-M2.7"


async def test_completion_cost_uses_pricing() -> None:
    """cost is computed from pricing_input/output * tokens (non-zero)."""
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        mock_litellm.completion = AsyncMock(
            return_value=_make_completion_response("ok", prompt_tokens=1000, completion_tokens=500)
        )
        router = LLMRouter()
        result = await router.completion(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
        )
        # MiniMax: 0.30/M input, 0.60/M output
        # 1000 input / 1e6 * 0.30 = 0.0003
        # 500 output / 1e6 * 0.60 = 0.0003
        # total = 0.0006
        assert result.cost > 0
        assert abs(result.cost - 0.0006) < 1e-9


# === streaming_completion() ===

async def test_streaming_yields_chunks() -> None:
    """streaming_completion is an async iterator yielding StreamEvent chunks."""
    chunks = _make_stream_chunks("Hello")
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        # litellm.completion(stream=True) returns a sync iterable of chunks
        mock_litellm.completion = MagicMock(return_value=iter(chunks))
        router = LLMRouter()

        events = []
        async for ev in router.streaming_completion(
            messages=[{"role": "user", "content": "Hi"}],
            model="MiniMax-M2.7",
        ):
            events.append(ev)

        # At least one event yielded
        assert len(events) >= 1
        # Concatenate tokens to get the full content
        content = "".join(ev.content for ev in events if ev.type == "token")
        assert content == "Hello"


async def test_streaming_includes_done_event() -> None:
    """Last event is a 'done' type."""
    chunks = _make_stream_chunks("OK")
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        mock_litellm.completion = MagicMock(return_value=iter(chunks))
        router = LLMRouter()

        events = []
        async for ev in router.streaming_completion(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
        ):
            events.append(ev)

        assert events[-1].type == "done"


# === model id mapping (provider prefix) ===

def test_to_litellm_model_id_prefixes_catalog_id() -> None:
    """Catalog id 'MiniMax-M2.7' maps to 'minimax/MiniMax-M2.7'."""
    assert LLMRouter._to_litellm_model_id("MiniMax-M2.7") == "minimax/MiniMax-M2.7"
    assert LLMRouter._to_litellm_model_id("glm-4.7") == "zhipuai/glm-4.7"
    assert LLMRouter._to_litellm_model_id("moonshot-v1-128k") == "moonshot/moonshot-v1-128k"


def test_to_litellm_model_id_passes_through_prefixed() -> None:
    """Already-prefixed id is passed through unchanged."""
    assert (
        LLMRouter._to_litellm_model_id("openai/gpt-4o")
        == "openai/gpt-4o"
    )
    assert (
        LLMRouter._to_litellm_model_id("anthropic/claude-3-5-sonnet")
        == "anthropic/claude-3-5-sonnet"
    )


def test_to_litellm_model_id_passes_through_unknown() -> None:
    """Unknown model id is passed through (let litellm error)."""
    assert (
        LLMRouter._to_litellm_model_id("unknown-model-xyz")
        == "unknown-model-xyz"
    )


async def test_completion_uses_prefixed_model() -> None:
    """completion() sends 'minimax/MiniMax-M2.7' to litellm, not bare id."""
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        mock_litellm.completion = AsyncMock(
            return_value=_make_completion_response("ok", 1, 1)
        )
        router = LLMRouter()
        await router.completion(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
        )
        # The model id sent to litellm MUST have the provider prefix,
        # otherwise litellm raises "LLM Provider NOT provided".
        assert (
            mock_litellm.completion.call_args.kwargs["model"]
            == "minimax/MiniMax-M2.7"
        )


# === import-time error ===

def test_router_handles_missing_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """If litellm is not importable, constructing LLMRouter raises RuntimeError.

    We simulate "litellm not installed" by flipping the router module's
    `_LITELLM_AVAILABLE` flag and clearing its cached `litellm` reference.
    """
    import harness.server.llm.router as router_mod

    # Save & flip
    original_available = router_mod._LITELLM_AVAILABLE
    original_litellm = router_mod.litellm
    monkeypatch.setattr(router_mod, "_LITELLM_AVAILABLE", False)
    monkeypatch.setattr(router_mod, "litellm", None)

    with pytest.raises(RuntimeError, match="litellm"):
        LLMRouter()

    # Restore via monkeypatch (auto-cleanup on test teardown)
    monkeypatch.setattr(router_mod, "_LITELLM_AVAILABLE", original_available)
    monkeypatch.setattr(router_mod, "litellm", original_litellm)
