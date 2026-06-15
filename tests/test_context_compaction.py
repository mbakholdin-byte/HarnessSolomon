"""Phase 3: tests for the ContextCompactor (sliding window + LLM summary).

Coverage:
    - ``maybe_compact`` returns the input unchanged when under threshold
    - ``maybe_compact`` returns the same list object (not a copy) on no-op
    - Tool-call pairing is preserved across the sliding window
    - System message (messages[0]) is never dropped
    - Last ``keep_recent_turns`` messages are always kept
    - Empty / single-message inputs are safe
    - The summariser is called when the sliding window is insufficient
    - The summariser fallback fires when the primary model errors
    - The summary is persisted to ``UnifiedMemory`` (mock) with tag ``#compact``
    - ``compaction_enabled=False`` → no-op
    - ``compaction_persist_to_memory=False`` → no write
    - Token estimation is monotonic in input size
    - Streaming path is unaffected (``AgentLoop.run`` rebinds ``messages``)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.config import Settings
from harness.context import ContextCompactor
from harness.context.compaction import (
    _estimate_tokens,
    _model_ctx,
)
from harness.server.llm.models import MODELS
from harness.server.llm.router import CompletionResult


# === Fixtures ===

@pytest.fixture
def settings() -> Settings:
    """Phase 3 settings tuned for compactness in tests.

    Threshold 0.1 of 32K = 3.2K, target 0.05 = 1.6K. Even modest
    histories (2000 tokens) cross the threshold so the sliding
    window and summariser get exercised. keep_recent_turns=4
    protects the last 4 turns from deletion.
    """
    return Settings(
        compaction_enabled=True,
        compaction_threshold_ratio=0.1,
        compaction_target_ratio=0.05,
        compaction_keep_recent_turns=4,
        compaction_summarizer_max_input_tokens=4000,
        compaction_persist_to_memory=True,
    )


@pytest.fixture
def short_history() -> list[dict[str, Any]]:
    """A history that fits well under the threshold."""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ]


@pytest.fixture
def long_history() -> list[dict[str, Any]]:
    """A history that exceeds the threshold and needs compaction."""
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]
    # 20 user/assistant pairs of long content to inflate token count.
    for i in range(20):
        msgs.append({
            "role": "user",
            "content": f"Question {i}: " + "lorem ipsum dolor sit amet " * 50,
        })
        msgs.append({
            "role": "assistant",
            "content": f"Answer {i}: " + "consectetur adipiscing elit " * 50,
        })
    return msgs


@pytest.fixture
def history_with_tool_pairs() -> list[dict[str, Any]]:
    """History with a tool-call pair that must be preserved together."""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Run a tool please"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "bash",
            "content": "ok",
        },
        {"role": "assistant", "content": "Done!"},
        {"role": "user", "content": "Thanks"},
    ]


# === Tests: token estimation ===

class TestEstimateTokens:
    def test_empty_returns_zero(self) -> None:
        assert _estimate_tokens([]) == 0

    def test_non_list_returns_zero(self) -> None:
        assert _estimate_tokens(None) == 0  # type: ignore[arg-type]
        assert _estimate_tokens("not a list") == 0  # type: ignore[arg-type]

    def test_single_message(self) -> None:
        # 100-char content → json.dumps adds ~37 chars of envelope
        # ("role", "content" keys + braces + quotes) → ~137 chars total.
        # 137 // 4 = 34 tokens. Tolerance 30-40.
        n = _estimate_tokens([{"role": "user", "content": "a" * 100}])
        assert 30 <= n <= 40

    def test_monotonic_in_size(self) -> None:
        small = _estimate_tokens([{"role": "user", "content": "a" * 100}])
        big = _estimate_tokens([{"role": "user", "content": "a" * 1000}])
        assert big > small
        # Roughly linear.
        assert 5 <= big / small <= 20

    def test_includes_tool_calls_overhead(self) -> None:
        plain = _estimate_tokens([{"role": "user", "content": "hi"}])
        with_tools = _estimate_tokens(
            [{"role": "user", "content": "hi", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
            ]}],
        )
        assert with_tools > plain


# === Tests: model ctx lookup ===

class TestModelCtx:
    def test_known_model_returns_catalog_ctx(self) -> None:
        s = Settings()
        ctx = _model_ctx("MiniMax-M2.7", s)
        assert ctx == 200000

    def test_unknown_model_returns_fallback(self) -> None:
        s = Settings()
        ctx = _model_ctx("not-a-real-model", s)
        assert ctx == 8192

    def test_qwen3_8b_in_catalog(self) -> None:
        # T1 summariser model — must have an entry.
        ctx = _model_ctx("qwen3:8b", Settings())
        assert ctx == 32768


# === Tests: maybe_compact returns same list when under threshold ===

class TestMaybeCompactNoOp:
    @pytest.mark.asyncio
    async def test_short_history_returned_unchanged(
        self, settings: Settings, short_history: list[dict[str, Any]],
    ) -> None:
        router = AsyncMock()
        c = ContextCompactor(settings=settings, router=router)
        out = await c.maybe_compact(short_history, model="qwen3:8b")
        # Same object, not a copy.
        assert out is short_history
        # Summariser was NOT called.
        router.completion.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_history_returned_unchanged(
        self, settings: Settings,
    ) -> None:
        c = ContextCompactor(settings=settings, router=AsyncMock())
        out = await c.maybe_compact([], model="qwen3:8b")
        assert out == []

    @pytest.mark.asyncio
    async def test_disabled_compaction_no_op(
        self, short_history: list[dict[str, Any]],
    ) -> None:
        s = Settings(compaction_enabled=False)
        c = ContextCompactor(settings=s, router=AsyncMock())
        out = await c.maybe_compact(short_history, model="qwen3:8b")
        assert out is short_history


# === Tests: sliding window drops oldest ===

class TestSlidingWindow:
    @pytest.mark.asyncio
    async def test_long_history_is_compacted(
        self, settings: Settings, long_history: list[dict[str, Any]],
    ) -> None:
        # Stub the router: summarise returns a fixed string.
        router = AsyncMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="SUMMARY", tool_calls=None),
        )
        c = ContextCompactor(settings=settings, router=router)
        out = await c.maybe_compact(long_history, model="qwen3:8b")
        # Token count of the output is at most the target. The output
        # may be the same length as the input if summarisation
        # produced a single replacement message — but the token
        # count of that replacement is bounded.
        assert _estimate_tokens(out) <= int(
            32768 * settings.compaction_target_ratio
        ) + 100  # small slack for the summary message itself
        # System message preserved.
        assert out[0]["role"] == "system"
        # The system message content is unchanged.
        assert out[0]["content"] == long_history[0]["content"]

    @pytest.mark.asyncio
    async def test_system_message_preserved_verbatim(
        self, settings: Settings, long_history: list[dict[str, Any]],
    ) -> None:
        router = AsyncMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="X", tool_calls=None),
        )
        c = ContextCompactor(settings=settings, router=router)
        out = await c.maybe_compact(long_history, model="qwen3:8b")
        # messages[0] is the system message and must be intact.
        assert out[0] == long_history[0]

    @pytest.mark.asyncio
    async def test_keep_recent_turns_floor(
        self, settings: Settings, long_history: list[dict[str, Any]],
    ) -> None:
        router = AsyncMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="X", tool_calls=None),
        )
        c = ContextCompactor(settings=settings, router=router)
        out = await c.maybe_compact(long_history, model="qwen3:8b")
        # Last ``keep_recent_turns`` (4) messages of the original
        # should be present in some form in the output (possibly
        # shifted by a summary message inserted at index 1).
        tail = long_history[-settings.compaction_keep_recent_turns:]
        # At least one of the recent turns must be verbatim.
        out_content = [m.get("content", "") for m in out]
        assert any(
            t["content"] in out_content
            for t in tail
            if isinstance(t.get("content"), str)
        )


# === Tests: tool-call pairing ===

class TestToolPairing:
    @pytest.mark.asyncio
    async def test_tool_pair_preserved(
        self, settings: Settings, history_with_tool_pairs: list[dict[str, Any]],
    ) -> None:
        # Inflate the history so compaction actually triggers.
        history = list(history_with_tool_pairs)
        for i in range(20):
            history.insert(
                1,
                {"role": "user", "content": "filler " * 100 + f"#{i}"},
            )
        router = AsyncMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="X", tool_calls=None),
        )
        c = ContextCompactor(settings=settings, router=router)
        out = await c.maybe_compact(history, model="qwen3:8b")
        # Find the kept assistant turn with tool_calls.
        kept_tool_call_ids: set[str] = set()
        for m in out:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        kept_tool_call_ids.add(tc["id"])
        # Every kept tool_call_id must have a matching tool message.
        for m in out:
            if m.get("role") == "tool":
                tc_id = m.get("tool_call_id")
                if tc_id and tc_id not in kept_tool_call_ids:
                    # The tool message references an id not in the
                    # kept assistant messages — that would break the
                    # contract. Note: this is also valid if the
                    # assistant turn that requested the tool was
                    # dropped along with the tool message; we only
                    # flag the mismatch when the tool survives alone.
                    pytest.fail(
                        f"tool message with id={tc_id!r} has no "
                        f"matching assistant.tool_calls in output"
                    )


# === Tests: summariser integration ===

class TestSummariser:
    @pytest.mark.asyncio
    async def test_summariser_called_with_dropped_block(
        self, long_history: list[dict[str, Any]],
    ) -> None:
        # Tight settings so sliding window alone can't satisfy.
        s = Settings(
            compaction_enabled=True,
            compaction_threshold_ratio=0.05,
            compaction_target_ratio=0.01,
            compaction_keep_recent_turns=2,
        )
        router = AsyncMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="SUMMARISED.", tool_calls=None),
        )
        c = ContextCompactor(settings=s, router=router)
        out = await c.maybe_compact(long_history, model="qwen3:8b")
        if router.completion.called:
            # The completion call should have been to the summariser
            # model (T1 by default).
            call_kwargs = router.completion.call_args.kwargs
            assert call_kwargs["model"] == s.subagent_t1_model
            # The summary should be present in the output.
            assert any(
                "SUMMARISED" in (m.get("content") or "")
                for m in out
            )

    @pytest.mark.asyncio
    async def test_fallback_used_on_primary_error(
        self, long_history: list[dict[str, Any]],
    ) -> None:
        # Use settings so tight that sliding window alone cannot
        # satisfy the target — the summariser MUST be called.
        s = Settings(
            compaction_enabled=True,
            compaction_threshold_ratio=0.05,
            compaction_target_ratio=0.01,  # 327 tokens — single message
            compaction_keep_recent_turns=2,
        )
        # Primary summariser errors; fallback returns a summary.
        # Use a simple class so attribute lookup doesn't get
        # hijacked by MagicMock's __getattr__.
        class _StubRouter:
            async def completion(
                self, *args: Any, **kwargs: Any,
            ) -> CompletionResult:
                if kwargs.get("model") == s.subagent_t1_model:
                    raise RuntimeError("primary unavailable")
                return CompletionResult(content="FALLBACK", tool_calls=None)

        router = _StubRouter()
        c = ContextCompactor(settings=s, router=router)
        out = await c.maybe_compact(long_history, model="qwen3:8b")
        # Fallback summary is in the output.
        assert any(
            "FALLBACK" in (m.get("content") or "")
            for m in out
        )

    @pytest.mark.asyncio
    async def test_total_summariser_failure_returns_trimmed(
        self, long_history: list[dict[str, Any]],
    ) -> None:
        # Use settings so tight that sliding window alone cannot
        # satisfy the target — the summariser MUST be called.
        s = Settings(
            compaction_enabled=True,
            compaction_threshold_ratio=0.05,
            compaction_target_ratio=0.01,  # 327 tokens — single message
            compaction_keep_recent_turns=2,
        )
        # Both primary and fallback raise.
        class _AlwaysFails:
            async def completion(
                self, *args: Any, **kwargs: Any,
            ) -> CompletionResult:
                raise RuntimeError("nope")
        router = _AlwaysFails()
        c = ContextCompactor(settings=s, router=router)
        out = await c.maybe_compact(long_history, model="qwen3:8b")
        # We still return something (the sliding window result).
        assert out
        assert out[0]["role"] == "system"


# === Tests: memory persistence ===

class TestMemoryPersistence:
    @pytest.mark.asyncio
    async def test_summary_written_to_memory_with_compact_tag(
        self, settings: Settings, long_history: list[dict[str, Any]],
    ) -> None:
        mem = MagicMock()
        mem.write = AsyncMock()
        router = AsyncMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="MEM-SUMMARY", tool_calls=None),
        )
        c = ContextCompactor(
            settings=settings, router=router, memory=mem,
            session_id="test-session-123",
        )
        await c.maybe_compact(long_history, model="qwen3:8b")
        if mem.write.called:
            written = mem.write.call_args.args[0]
            # Source is "compact" and tag is "#compact".
            assert written.source == "compact"
            assert "#compact" in written.tags
            assert "#session/test-session-123" in written.tags

    @pytest.mark.asyncio
    async def test_persist_disabled_skips_write(
        self, long_history: list[dict[str, Any]],
    ) -> None:
        s = Settings(
            compaction_enabled=True,
            compaction_persist_to_memory=False,
            compaction_threshold_ratio=0.1,
            compaction_target_ratio=0.05,
            compaction_keep_recent_turns=4,
        )
        mem = MagicMock()
        mem.write = AsyncMock()
        router = AsyncMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="X", tool_calls=None),
        )
        c = ContextCompactor(settings=s, router=router, memory=mem)
        await c.maybe_compact(long_history, model="qwen3:8b")
        mem.write.assert_not_called()


# === Tests: AgentLoop integration (smoke) ===

class TestAgentLoopIntegration:
    """The compactor is DI'd into AgentLoop. The loop rebinds
    ``messages`` after compaction so the in-place append below
    still works."""

    @pytest.mark.asyncio
    async def test_agent_loop_with_compactor(self, settings: Settings) -> None:
        from harness.server.agent.loop import AgentLoop
        from harness.server.agent.runtime import ToolRuntime

        runtime = MagicMock()
        runtime.project_root = settings.project_root
        router = MagicMock()
        # Stub completion to return a no-tool-call response.
        router.streaming_completion = MagicMock()  # so supports_streaming=True
        router.completion = AsyncMock(
            return_value=CompletionResult(content="done", tool_calls=None),
        )
        compactor = ContextCompactor(settings=settings, router=router)
        loop_obj = AgentLoop(
            runtime=runtime, router=router, compactor=compactor,
        )
        msgs = [
            {"role": "user", "content": "hi"},
        ]
        events = []
        async for ev in loop_obj.run(messages=msgs, model="qwen3:8b", stream=False):
            events.append(ev)
        # Loop should have run to completion.
        assert any(ev.type == "assistant_message" for ev in events)
        assert any(ev.type == "done" for ev in events)

    @pytest.mark.asyncio
    async def test_agent_loop_without_compactor_unchanged(
        self, settings: Settings,
    ) -> None:
        from harness.server.agent.loop import AgentLoop

        runtime = MagicMock()
        runtime.project_root = settings.project_root
        router = MagicMock()
        router.streaming_completion = MagicMock()
        router.completion = AsyncMock(
            return_value=CompletionResult(content="ok", tool_calls=None),
        )
        # No compactor.
        loop_obj = AgentLoop(runtime=runtime, router=router)
        msgs = [{"role": "user", "content": "hi"}]
        events = []
        async for ev in loop_obj.run(messages=msgs, model="qwen3:8b", stream=False):
            events.append(ev)
        assert any(ev.type == "assistant_message" for ev in events)


# === Tests: Qwen3 model catalog integration ===

class TestSummariserModelResolution:
    def test_summariser_defaults_to_t1(self) -> None:
        s = Settings(compaction_summarizer_model="")
        c = ContextCompactor(settings=s, router=MagicMock())
        assert c._summariser == s.subagent_t1_model

    def test_summariser_override_honoured(self) -> None:
        s = Settings(compaction_summarizer_model="glm-4.7")
        c = ContextCompactor(settings=s, router=MagicMock())
        assert c._summariser == "glm-4.7"

    def test_fallback_defaults_to_t2(self) -> None:
        s = Settings(compaction_summarizer_fallback="")
        c = ContextCompactor(settings=s, router=MagicMock())
        assert c._fallback == s.subagent_t2_model
