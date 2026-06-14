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


# === tool schema wrapping ===

def test_wrap_tools_unwrapped_to_wrapped() -> None:
    """Tool in 'name/description/parameters' form is wrapped to OpenAI shape."""
    unwrapped = [
        {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    wrapped = LLMRouter._wrap_tools_for_litellm(unwrapped)
    assert wrapped == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]


def test_wrap_tools_passes_through_already_wrapped() -> None:
    """Tool already in OpenAI wrapped form is passed through unchanged."""
    wrapped_in = [
        {
            "type": "function",
            "function": {
                "name": "x",
                "description": "y",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert LLMRouter._wrap_tools_for_litellm(wrapped_in) == wrapped_in


def test_wrap_tools_handles_none_and_empty() -> None:
    """None and empty list pass through unchanged."""
    assert LLMRouter._wrap_tools_for_litellm(None) is None
    assert LLMRouter._wrap_tools_for_litellm([]) == []


def test_wrap_tools_mixed_wrapped_and_unwrapped() -> None:
    """Mix of wrapped and unwrapped tools — each handled correctly."""
    tools = [
        {"name": "a", "description": "A", "parameters": {"type": "object"}},
        {
            "type": "function",
            "function": {
                "name": "b",
                "description": "B",
                "parameters": {"type": "object"},
            },
        },
    ]
    wrapped = LLMRouter._wrap_tools_for_litellm(tools)
    assert wrapped[0]["type"] == "function"
    assert wrapped[0]["function"]["name"] == "a"
    assert wrapped[1] == tools[1]  # pass-through


async def test_completion_wraps_unwrapped_tools() -> None:
    """completion() wraps unwrapped tools before sending to litellm.

    Without this, litellm's minimax provider sends bare schemas to the
    MiniMax API, which rejects with 'invalid tool type:'.
    """
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        mock_litellm.completion = AsyncMock(
            return_value=_make_completion_response("ok", 1, 1)
        )
        router = LLMRouter()
        unwrapped_tools = [
            {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ]
        await router.completion(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
            tools=unwrapped_tools,
        )
        # The tools sent to litellm MUST be in OpenAI wrapped form
        sent_tools = mock_litellm.completion.call_args.kwargs["tools"]
        assert sent_tools[0]["type"] == "function"
        assert sent_tools[0]["function"]["name"] == "read_file"
        assert sent_tools[0]["function"]["parameters"] == unwrapped_tools[0]["parameters"]


# === per-model tool limit ===
#
# Live verification 2026-06-14: MiniMax-M2.7 accepts 32 tools without
# error when schemas are wrapped in OpenAI shape (the original 2013
# error was the missing wrap, not the count). We bump the per-model
# cap from 4 → 16 to fit the Phase 0/0.5 toolset with headroom.
# DEFAULT_MAX_TOOLS is also 16.

from harness.server.llm.models import DEFAULT_MAX_TOOLS, get_model

_MINIMAX_MAX = get_model("MiniMax-M2.7").max_tools  # 16 in current catalog


def test_limit_tools_passes_through_under_cap() -> None:
    """When tool count <= max_tools, all tools are passed through."""
    n = _MINIMAX_MAX  # exactly at the cap → passes through
    tools = [{"name": f"t_{i}", "description": "x", "parameters": {"type": "object"}} for i in range(n)]
    out = LLMRouter._limit_tools_for_model("MiniMax-M2.7", tools)
    assert out == tools
    assert len(out) == n


def test_limit_tools_truncates_at_cap() -> None:
    """When tool count > max_tools, only the first N are kept."""
    n_over = _MINIMAX_MAX + 4  # 20 tools > cap 16
    tools = [{"name": f"t_{i}", "description": "x", "parameters": {"type": "object"}} for i in range(n_over)]
    out = LLMRouter._limit_tools_for_model("MiniMax-M2.7", tools)
    assert len(out) == _MINIMAX_MAX
    assert [t["name"] for t in out] == [f"t_{i}" for i in range(_MINIMAX_MAX)]


def test_limit_tools_unknown_model_uses_default() -> None:
    """Unknown model id falls back to DEFAULT_MAX_TOOLS."""
    n_over = DEFAULT_MAX_TOOLS + 4
    tools = [{"name": f"t_{i}", "description": "x", "parameters": {"type": "object"}} for i in range(n_over)]
    out = LLMRouter._limit_tools_for_model("unknown-xyz-model", tools)
    assert len(out) == DEFAULT_MAX_TOOLS


def test_limit_tools_none_and_empty() -> None:
    """None and empty list are passed through unchanged."""
    assert LLMRouter._limit_tools_for_model("MiniMax-M2.7", None) is None
    assert LLMRouter._limit_tools_for_model("MiniMax-M2.7", []) == []


def test_limit_tools_logs_warning_on_truncation(caplog) -> None:
    """Truncation emits a warning naming the dropped tools."""
    import logging
    n_over = _MINIMAX_MAX + 2
    tools = [{"name": f"t_{i}", "description": "x", "parameters": {"type": "object"}} for i in range(n_over)]
    with caplog.at_level(logging.WARNING, logger="harness.server.llm.router"):
        LLMRouter._limit_tools_for_model("MiniMax-M2.7", tools)
    warnings = [r for r in caplog.records if "truncated" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    # The last 2 tool names should appear in the dropped-tools list
    assert f"t_{n_over - 2}" in msg and f"t_{n_over - 1}" in msg


async def test_completion_truncates_tools_per_model() -> None:
    """completion() truncates tools to the model's max_tools limit.

    Integration test: send 20 tools to MiniMax (max=16), verify litellm
    receives only 16, all in OpenAI-wrapped form.
    """
    n_over = _MINIMAX_MAX + 4
    with patch("harness.server.llm.router.litellm") as mock_litellm:
        mock_litellm.completion = AsyncMock(
            return_value=_make_completion_response("ok", 1, 1)
        )
        router = LLMRouter()
        tools = [
            {"name": f"t_{i}", "description": "x",
             "parameters": {"type": "object", "properties": {}}}
            for i in range(n_over)
        ]
        await router.completion(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
            tools=tools,
        )
        sent_tools = mock_litellm.completion.call_args.kwargs["tools"]
        # MiniMax max_tools → only first N reach litellm
        assert len(sent_tools) == _MINIMAX_MAX
        assert all(t.get("type") == "function" for t in sent_tools)
        # And they're wrapped (truncation + wrapping both applied)
        assert [t["function"]["name"] for t in sent_tools] == [
            f"t_{i}" for i in range(_MINIMAX_MAX)
        ]


# === truncation metric (Phase 0+ in-process counter, Phase 4 → Prometheus) ===

def test_truncation_counter_starts_at_zero() -> None:
    """get_truncation_counts() returns empty after reset (or any model absent)."""
    from harness.server.llm.router import get_truncation_counts, reset_truncation_counts

    reset_truncation_counts()
    counts = get_truncation_counts()
    # Either empty (clean state) or only models that haven't been touched
    # in this test should be 0. We don't assert exact zero — other tests in
    # the same process may have left entries. We DO assert it's a dict.
    assert isinstance(counts, dict)
    assert counts.get("MiniMax-M2.7-test-marker", 0) == 0


def test_truncation_counter_increments_on_truncate() -> None:
    """Each call to _limit_tools_for_model that actually truncates bumps the counter."""
    from harness.server.llm.router import (
        get_truncation_counts,
        reset_truncation_counts,
    )

    reset_truncation_counts()
    marker = "test-counter-marker-xyz"
    n_over = _MINIMAX_MAX + 3
    tools = [
        {"name": f"t_{i}", "description": "x", "parameters": {"type": "object"}}
        for i in range(n_over)
    ]
    # 3 calls → counter should be 3 for that model
    for _ in range(3):
        LLMRouter._limit_tools_for_model(marker, tools)
    counts = get_truncation_counts()
    assert counts.get(marker, 0) == 3


def test_truncation_counter_does_not_increment_under_cap() -> None:
    """When tool count <= cap, the counter is NOT bumped."""
    from harness.server.llm.router import (
        get_truncation_counts,
        reset_truncation_counts,
    )

    reset_truncation_counts()
    marker = "test-counter-uncapped-xyz"
    tools = [{"name": f"t_{i}", "description": "x", "parameters": {"type": "object"}} for i in range(3)]
    LLMRouter._limit_tools_for_model(marker, tools)
    assert get_truncation_counts().get(marker, 0) == 0


def test_truncation_counter_per_model_isolation() -> None:
    """Counter increments only for the model that actually truncated."""
    from harness.server.llm.router import (
        get_truncation_counts,
        reset_truncation_counts,
    )

    reset_truncation_counts()
    m1 = "test-iso-model-aaa"
    m2 = "test-iso-model-bbb"
    n_over = _MINIMAX_MAX + 1
    tools = [{"name": f"t_{i}", "description": "x", "parameters": {"type": "object"}} for i in range(n_over)]
    LLMRouter._limit_tools_for_model(m1, tools)
    LLMRouter._limit_tools_for_model(m2, tools)
    LLMRouter._limit_tools_for_model(m1, tools)
    counts = get_truncation_counts()
    assert counts.get(m1, 0) == 2
    assert counts.get(m2, 0) == 1


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
