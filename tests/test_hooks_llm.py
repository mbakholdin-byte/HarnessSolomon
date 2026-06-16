"""Phase 4.0: Tests for LLM-as-hook transport (DI to LLMRouter)."""
from __future__ import annotations

import asyncio

import pytest

from harness.hooks.context import HookContext
from harness.hooks.llm_hook import LLMHook, _extract_json_decision


class FakeRouter:
    """Fake LLMRouter for testing — records calls and returns canned response."""

    def __init__(self, response: str | object) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def completion(self, *, messages: list[dict], model: str) -> object:
        self.calls.append({"messages": messages, "model": model})
        return self._response


class TestExtractJsonDecision:
    """_extract_json_decision parses JSON from LLM output (permissive)."""

    def test_full_json(self) -> None:
        text = '{"decision": "block", "reason": "x"}'
        d = _extract_json_decision(text)
        assert d == {"decision": "block", "reason": "x"}

    def test_json_with_surrounding_text(self) -> None:
        text = 'Sure! Here is my answer: {"decision": "allow"} thanks'
        d = _extract_json_decision(text)
        assert d == {"decision": "allow"}

    def test_invalid_returns_none(self) -> None:
        assert _extract_json_decision("just plain text") is None
        assert _extract_json_decision("") is None

    def test_nested_json_handled(self) -> None:
        """Nested objects in raw text: regex fallback doesn't catch them.

        Plan C1: regex `[^{}]*decision[^{}]*` is a fallback for
        non-JSON outputs from small models. Nested objects (with
        extra braces) are NOT supported by the regex. This is
        acceptable: most LLMs emit valid JSON when prompted, which
        is handled by the primary ``json.loads`` path.
        """
        text = 'Reasoning: {"nested": {"k": "v"}, "decision": "modify", "reason": "x"}'
        d = _extract_json_decision(text)
        # Regex doesn't find decision (nested braces break it), so returns None.
        # This is a known limitation; production usage relies on the LLM
        # emitting valid JSON.
        assert d is None

    def test_nested_json_full_parse(self) -> None:
        """When the LLM emits valid JSON with nested objects, the primary path works."""
        text = '{"decision": "modify", "payload": {"nested": {"k": "v"}}}'
        d = _extract_json_decision(text)
        assert d is not None
        assert d.get("decision") == "modify"
        assert d.get("payload") == {"nested": {"k": "v"}}


class TestLLMHookAllow:
    """LLM returns 'allow' decision."""

    async def test_str_response_with_json(self) -> None:
        router = FakeRouter('{"decision": "allow", "reason": "safe"}')
        hook = LLMHook(router, model="test-model", prompt="decide: {event}")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "allow"
        assert d.hook_id == "llm.test-model"
        assert d.output == {"reason": "safe"}


class TestLLMHookBlock:
    """LLM returns 'block' decision."""

    async def test_block(self) -> None:
        router = FakeRouter('{"decision": "block", "reason": "dangerous"}')
        hook = LLMHook(router, model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "block"


class TestLLMHookModify:
    """LLM returns 'modify' decision with payload."""

    async def test_modify(self) -> None:
        router = FakeRouter('{"decision": "modify", "payload": {"k": "v"}}')
        hook = LLMHook(router, model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "modify"
        assert d.output == {"payload": {"k": "v"}}


class TestLLMHookErrors:
    """LLM hook errors fail open."""

    async def test_invalid_decision_string_fails_open(self) -> None:
        router = FakeRouter('{"decision": "maybe", "reason": "x"}')
        hook = LLMHook(router, model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "allow"  # default fallback

    async def test_no_json_fails_open(self) -> None:
        router = FakeRouter("just text")
        hook = LLMHook(router, model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "allow"
        assert "could not parse" in d.error

    async def test_router_exception_fails_open(self) -> None:
        class ErrorRouter:
            async def completion(self, *, messages, model) -> None:
                raise RuntimeError("router down")

        hook = LLMHook(ErrorRouter(), model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "allow"
        assert "RuntimeError" in d.error

    async def test_router_timeout_fails_open(self) -> None:
        class SlowRouter:
            async def completion(self, *, messages, model) -> None:
                await asyncio.sleep(5)
                return "ok"

        hook = LLMHook(SlowRouter(), model="m", prompt="p", timeout_ms=100)
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "allow"
        assert "timeout" in d.error


class TestLLMHookResponseParsing:
    """LLMHook extracts text from various response shapes."""

    async def test_object_with_content(self) -> None:
        class Obj:
            content = '{"decision": "allow"}'

        router = FakeRouter(Obj())
        hook = LLMHook(router, model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "allow"

    async def test_object_with_text(self) -> None:
        class Obj:
            text = '{"decision": "block"}'

        router = FakeRouter(Obj())
        hook = LLMHook(router, model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "block"

    async def test_router_called_with_correct_args(self) -> None:
        router = FakeRouter('{"decision": "allow"}')
        hook = LLMHook(
            router, model="qwen3-8b", prompt="Decide for {event}: {payload}"
        )
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="a1",
            payload={"tool_name": "read_file"},
        )
        await hook(ctx)
        assert len(router.calls) == 1
        call = router.calls[0]
        assert call["model"] == "qwen3-8b"
        # System message is the instruction.
        assert "hook" in call["messages"][0]["content"].lower()
        # User message is the rendered prompt.
        user_msg = call["messages"][1]["content"]
        assert "PreToolUse" in user_msg
        assert "read_file" in user_msg


class TestLLMHookReasonCap:
    """LLM hook output bounded (defence in depth)."""

    async def test_long_reason_truncated(self) -> None:
        long_reason = "x" * 1000
        router = FakeRouter(f'{{"decision": "block", "reason": "{long_reason}"}}')
        hook = LLMHook(router, model="m", prompt="p")
        ctx = HookContext(
            event="PreToolUse", session_id="s1", agent_id="", payload={}
        )
        d = await hook(ctx)
        assert d.decision == "block"
        assert len(d.output["reason"]) <= 200
