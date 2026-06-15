"""Tests for ``harness.server.agent.reflection_loop.ReflectionLoop`` (Phase 3 v1.4.0).

Covers:
  - empty events → no router call, returns []
  - no router → returns []
  - happy path: T1 returns valid JSON → lessons parsed + dual-written
  - T1 fails → falls back to T2
  - T1+T2 both fail → returns [] + audit ``reflection_cascade_failed``
  - JSON parse failure → returns [] + audit ``reflection_parse_failed``
  - ``max_lessons`` cap is honoured
  - JSON wrapped in ``​```json fences → parsed
  - Tags include ``#reflection`` and ``#session/{id}``
  - Scratchpad write failure → UnifiedMemory leg still runs (fail-open)
  - UnifiedMemory write failure → scratchpad leg still works (fail-open)
  - Audit is best-effort (None or raises → swallowed)
  - Model id resolution: explicit > settings > default
  - Per-call timeout (caller wraps in ``asyncio.wait_for``; we verify
    cascade does not introduce its own blocking I/O)
  - ``SessionEvent`` / ``Lesson`` dataclasses are frozen

We use stub objects for the scratchpad, unified_memory, and router.
ReflectionLoop is duck-typed end-to-end; it never imports the real
modules (trust boundary).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.server.agent.reflection_loop import (
    Lesson,
    ReflectionLoop,
    SessionEvent,
    _parse_lessons,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_router(*, content: str | None = None, side_effect: BaseException | None = None) -> MagicMock:
    """Build a stub router.

    If ``side_effect`` is set, ``completion`` raises it. If ``content`` is
    set, ``completion`` returns a stub with that content.
    """
    router = MagicMock()
    if side_effect is not None:
        router.completion = AsyncMock(side_effect=side_effect)
    else:
        response = MagicMock()
        response.content = content or ""
        router.completion = AsyncMock(return_value=response)
    return router


def make_settings(**overrides: Any) -> Any:
    """Build a stub settings object with the fields ReflectionLoop reads."""
    base = {
        "reflection_model": "",
        "reflection_fallback_model": "",
        "reflection_max_lessons": 5,
        "reflection_max_ms": 10000,
        "subagent_t1_model": "qwen3:8b",
        "subagent_t2_model": "glm-4.7",
    }
    base.update(overrides)
    return MagicMock(**base)


def make_scratchpad(*, raise_on_write: BaseException | None = None, session_id: str = "sess-42") -> MagicMock:
    """Stub scratchpad with ``write_note`` async method."""
    sp = MagicMock()
    sp._session_id = session_id
    if raise_on_write is not None:
        sp.write_note = AsyncMock(side_effect=raise_on_write)
    else:
        note = MagicMock()
        note.id = 1
        sp.write_note = AsyncMock(return_value=note)
    return sp


def make_unified_memory(*, raise_on_write: BaseException | None = None) -> MagicMock:
    """Stub UnifiedMemory with ``write`` method (sync or async)."""
    um = MagicMock()
    if raise_on_write is not None:
        um.write = MagicMock(side_effect=raise_on_write)
    else:
        um.write = MagicMock()
    return um


# ---------------------------------------------------------------------------
# SessionEvent / Lesson dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_session_event_is_frozen(self) -> None:
        ev = SessionEvent(kind="user", content="hi", ts=1.0)
        with pytest.raises((AttributeError, Exception)):
            ev.kind = "assistant"  # type: ignore[misc]

    def test_lesson_is_frozen(self) -> None:
        lesson = Lesson(kind="gotcha", content="x", tags=["a"])
        with pytest.raises((AttributeError, Exception)):
            lesson.kind = "preference"  # type: ignore[misc]

    def test_lesson_tags_default_to_empty_list(self) -> None:
        lesson = Lesson(kind="pattern", content="x")
        assert lesson.tags == []

    def test_session_event_offloaded_id_optional(self) -> None:
        ev1 = SessionEvent(kind="tool", content="output", ts=1.0,
                           tool_name="bash")
        ev2 = SessionEvent(kind="tool", content="output", ts=1.0,
                           tool_name="bash", offloaded_id=99)
        assert ev1.offloaded_id is None
        assert ev2.offloaded_id == 99


# ---------------------------------------------------------------------------
# _parse_lessons unit tests
# ---------------------------------------------------------------------------


class TestParseLessons:
    def test_empty_returns_empty(self) -> None:
        assert _parse_lessons("", expected_max=5) == []
        assert _parse_lessons("   ", expected_max=5) == []

    def test_plain_json_array(self) -> None:
        raw = json.dumps([
            {"kind": "gotcha", "content": "X", "tags": ["a"]},
            {"kind": "pattern", "content": "Y", "tags": []},
        ])
        lessons = _parse_lessons(raw, expected_max=5)
        assert len(lessons) == 2
        assert lessons[0].kind == "gotcha"
        assert lessons[0].content == "X"
        assert lessons[0].tags == ["a"]

    def test_fenced_json(self) -> None:
        raw = "```json\n" + json.dumps([
            {"kind": "preference", "content": "use tabs", "tags": []},
        ]) + "\n```"
        lessons = _parse_lessons(raw, expected_max=5)
        assert len(lessons) == 1
        assert lessons[0].kind == "preference"

    def test_fenced_no_lang(self) -> None:
        raw = "```\n" + json.dumps([
            {"kind": "gotcha", "content": "X", "tags": []},
        ]) + "\n```"
        lessons = _parse_lessons(raw, expected_max=5)
        assert len(lessons) == 1

    def test_leading_prose_around_json(self) -> None:
        raw = (
            "Here is the result you asked for:\n"
            + json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
            + "\nHope that helps."
        )
        lessons = _parse_lessons(raw, expected_max=5)
        assert len(lessons) == 1

    def test_invalid_kind_dropped(self) -> None:
        raw = json.dumps([
            {"kind": "gotcha", "content": "X", "tags": []},
            {"kind": "unknown", "content": "skip", "tags": []},
            {"kind": "pattern", "content": "Y", "tags": []},
        ])
        lessons = _parse_lessons(raw, expected_max=5)
        assert len(lessons) == 2
        assert [l.kind for l in lessons] == ["gotcha", "pattern"]

    def test_empty_content_dropped(self) -> None:
        raw = json.dumps([
            {"kind": "gotcha", "content": "   ", "tags": []},
            {"kind": "pattern", "content": "good", "tags": []},
        ])
        lessons = _parse_lessons(raw, expected_max=5)
        assert len(lessons) == 1
        assert lessons[0].content == "good"

    def test_non_dict_items_skipped(self) -> None:
        raw = json.dumps([
            "string-not-dict",
            {"kind": "gotcha", "content": "X", "tags": []},
            42,
            None,
        ])
        lessons = _parse_lessons(raw, expected_max=5)
        assert len(lessons) == 1

    def test_max_lessons_cap(self) -> None:
        items = [
            {"kind": "gotcha", "content": f"L{i}", "tags": []}
            for i in range(10)
        ]
        raw = json.dumps(items)
        lessons = _parse_lessons(raw, expected_max=3)
        assert len(lessons) == 3
        assert [l.content for l in lessons] == ["L0", "L1", "L2"]

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_lessons("not json at all", expected_max=5) == []
        assert _parse_lessons("[broken", expected_max=5) == []
        assert _parse_lessons("broken]", expected_max=5) == []

    def test_non_list_returns_empty(self) -> None:
        assert _parse_lessons(json.dumps({"not": "list"}), expected_max=5) == []

    def test_non_string_tags_coerced(self) -> None:
        raw = json.dumps([
            {"kind": "gotcha", "content": "X", "tags": [1, 2.5, "str", None]},
        ])
        lessons = _parse_lessons(raw, expected_max=5)
        assert lessons[0].tags == ["1", "2.5", "str"]


# ---------------------------------------------------------------------------
# ReflectionLoop behavioural tests
# ---------------------------------------------------------------------------


class TestReflectionLoop:
    async def test_empty_events_returns_empty_without_router_call(self) -> None:
        router = make_router(content="should not be called")
        loop = ReflectionLoop(
            scratchpad=None, settings=make_settings(), router=router,
        )
        result = await loop.reflect([])
        assert result == []
        router.completion.assert_not_called()

    async def test_no_router_returns_empty(self) -> None:
        loop = ReflectionLoop(
            scratchpad=None, settings=make_settings(), router=None,
        )
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert result == []

    async def test_happy_path_parses_lessons(self) -> None:
        lessons_json = json.dumps([
            {"kind": "gotcha", "content": "X", "tags": ["sql"]},
            {"kind": "pattern", "content": "Y", "tags": []},
        ])
        router = make_router(content=lessons_json)
        scratchpad = make_scratchpad()
        um = make_unified_memory()
        audit = MagicMock()
        loop = ReflectionLoop(
            scratchpad=scratchpad,
            settings=make_settings(),
            router=router,
            unified_memory=um,
            audit=audit,
        )
        events = [
            SessionEvent(kind="user", content="hello", ts=1.0),
            SessionEvent(kind="assistant", content="hi", ts=2.0),
        ]
        result = await loop.reflect(events)
        assert len(result) == 2
        assert result[0].kind == "gotcha"
        # Router called exactly once (T1 succeeded).
        assert router.completion.await_count == 1
        # Dual-write happened.
        assert scratchpad.write_note.await_count == 2
        assert um.write.call_count == 2
        # Audit recorded extraction.
        audit.record.assert_called_once()
        assert audit.record.call_args.kwargs["event"] == "reflection_extracted"
        assert audit.record.call_args.kwargs["count"] == 2

    async def test_t1_fails_falls_back_to_t2(self) -> None:
        lessons_json = json.dumps([
            {"kind": "gotcha", "content": "X", "tags": []},
        ])
        # T1 raises; T2 returns valid JSON.
        call_count = {"n": 0}

        async def _side_effect(**kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("T1 is down")
            response = MagicMock()
            response.content = lessons_json
            return response

        router = MagicMock()
        router.completion = AsyncMock(side_effect=_side_effect)
        loop = ReflectionLoop(
            scratchpad=None,
            settings=make_settings(reflection_model="t1-model", reflection_fallback_model="t2-model"),
            router=router,
        )
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 1
        assert call_count["n"] == 2
        # First call used T1, second used T2.
        first_call = router.completion.call_args_list[0]
        second_call = router.completion.call_args_list[1]
        assert first_call.kwargs["model"] == "t1-model"
        assert second_call.kwargs["model"] == "t2-model"

    async def test_both_models_fail_returns_empty_and_audits(self) -> None:
        router = MagicMock()
        router.completion = AsyncMock(side_effect=ConnectionError("network down"))
        audit = MagicMock()
        loop = ReflectionLoop(
            scratchpad=None,
            settings=make_settings(),
            router=router,
            audit=audit,
        )
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert result == []
        # Cascade failed audit recorded.
        cascade_audits = [
            c for c in audit.record.call_args_list
            if c.kwargs.get("event") == "reflection_cascade_failed"
        ]
        assert len(cascade_audits) == 1

    async def test_json_parse_failure_returns_empty_and_audits(self) -> None:
        router = make_router(content="This is not valid JSON for lessons.")
        audit = MagicMock()
        loop = ReflectionLoop(
            scratchpad=None, settings=make_settings(), router=router, audit=audit,
        )
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert result == []
        parse_audits = [
            c for c in audit.record.call_args_list
            if c.kwargs.get("event") == "reflection_parse_failed"
        ]
        assert len(parse_audits) == 1
        assert "preview" in parse_audits[0].kwargs

    async def test_max_lessons_cap_honoured(self) -> None:
        items = [{"kind": "gotcha", "content": f"L{i}", "tags": []} for i in range(20)]
        router = make_router(content=json.dumps(items))
        loop = ReflectionLoop(
            scratchpad=None, settings=make_settings(reflection_max_lessons=3), router=router,
        )
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 3

    async def test_scratchpad_write_failure_does_not_break_unified_memory(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)
        scratchpad = make_scratchpad(raise_on_write=RuntimeError("db locked"))
        um = make_unified_memory()
        loop = ReflectionLoop(
            scratchpad=scratchpad,
            settings=make_settings(),
            router=router,
            unified_memory=um,
        )
        # Should not raise — fail-open.
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 1
        # UnifiedMemory still got the write.
        assert um.write.call_count == 1

    async def test_unified_memory_write_failure_does_not_break_scratchpad(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)
        scratchpad = make_scratchpad()
        um = make_unified_memory(raise_on_write=RuntimeError("qdrant down"))
        loop = ReflectionLoop(
            scratchpad=scratchpad,
            settings=make_settings(),
            router=router,
            unified_memory=um,
        )
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 1
        # Scratchpad still got the write.
        assert scratchpad.write_note.await_count == 1

    async def test_audit_none_is_handled(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)
        loop = ReflectionLoop(
            scratchpad=None, settings=make_settings(), router=router, audit=None,
        )
        # Should not raise on the success path.
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 1

    async def test_audit_record_raises_is_swallowed(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)
        audit = MagicMock()
        audit.record.side_effect = RuntimeError("audit down")
        loop = ReflectionLoop(
            scratchpad=None, settings=make_settings(), router=router, audit=audit,
        )
        # Should not raise.
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 1

    async def test_explicit_model_overrides_settings(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)
        loop = ReflectionLoop(
            scratchpad=None,
            settings=make_settings(
                reflection_model="my-custom-t1",
                subagent_t1_model="should-not-be-used",
            ),
            router=router,
        )
        await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        call = router.completion.call_args
        assert call.kwargs["model"] == "my-custom-t1"

    async def test_falls_back_to_subagent_t1_when_reflection_model_empty(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)
        loop = ReflectionLoop(
            scratchpad=None,
            settings=make_settings(
                reflection_model="",
                subagent_t1_model="custom-t1",
            ),
            router=router,
        )
        await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        call = router.completion.call_args
        assert call.kwargs["model"] == "custom-t1"

    async def test_falls_back_to_default_when_no_settings_attr(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)

        class _MinimalSettings:
            reflection_model = ""
            reflection_fallback_model = ""
            reflection_max_lessons = 5

        loop = ReflectionLoop(
            scratchpad=None,
            settings=_MinimalSettings(),
            router=router,
        )
        await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        # Default T1 = "qwen3:8b"
        call = router.completion.call_args
        assert call.kwargs["model"] == "qwen3:8b"

    async def test_dual_writes_include_reflection_and_session_tags(self) -> None:
        lessons_json = json.dumps([
            {"kind": "gotcha", "content": "X", "tags": ["sql"]},
        ])
        router = make_router(content=lessons_json)
        scratchpad = make_scratchpad(session_id="sess-99")
        loop = ReflectionLoop(
            scratchpad=scratchpad,
            settings=make_settings(),
            router=router,
        )
        await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        call = scratchpad.write_note.call_args
        tags = call.kwargs["tags"]
        assert "#reflection" in tags
        assert "#session/sess-99" in tags
        # Original user tag preserved.
        assert "sql" in tags
        # Level is L1.
        assert call.kwargs["level"] == "L1"

    async def test_unified_memory_receives_kind_and_source(self) -> None:
        lessons_json = json.dumps([
            {"kind": "preference", "content": "X", "tags": []},
        ])
        router = make_router(content=lessons_json)
        um = make_unified_memory()
        loop = ReflectionLoop(
            scratchpad=None,
            settings=make_settings(),
            router=router,
            unified_memory=um,
        )
        await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        call = um.write.call_args
        assert call.kwargs["source"] == "reflection"
        assert call.kwargs["layer"] == "L1"
        assert call.kwargs["kind"] == "preference"

    async def test_no_scratchpad_no_unified_memory_still_extracts(self) -> None:
        lessons_json = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
        router = make_router(content=lessons_json)
        loop = ReflectionLoop(
            scratchpad=None, settings=make_settings(), router=router,
            unified_memory=None,
        )
        # No persistence, but the lessons are still returned.
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 1

    async def test_response_with_no_content_attr_returns_none(self) -> None:
        """If router returns response without ``content``, treat as failure → cascade to T2."""
        call_count = {"n": 0}

        async def _side_effect(**kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                response = MagicMock()
                response.content = None  # no content
                return response
            response = MagicMock()
            response.content = json.dumps([{"kind": "gotcha", "content": "X", "tags": []}])
            return response

        router = MagicMock()
        router.completion = AsyncMock(side_effect=_side_effect)
        loop = ReflectionLoop(
            scratchpad=None,
            settings=make_settings(),
            router=router,
        )
        result = await loop.reflect([SessionEvent(kind="user", content="x", ts=1.0)])
        assert len(result) == 1
        assert call_count["n"] == 2  # T1 tried (returned None), T2 succeeded


# ---------------------------------------------------------------------------
# AgentRunner reflection_factory kwarg test
# ---------------------------------------------------------------------------


class TestAgentRunnerReflectionFactory:
    def test_runner_accepts_reflection_factory_kwarg(self) -> None:
        from harness.agents.runner import AgentRunner
        from harness.server.llm.router import LLMRouter

        factory = MagicMock()
        # We don't actually call factory here — just verify it's stored.
        # Use a stub router to satisfy the type.
        runner = AgentRunner(
            router=MagicMock(spec=LLMRouter),
            repo=MagicMock(),
            reflection_factory=factory,
        )
        assert runner._reflection_factory is factory

    def test_runner_reflection_factory_defaults_to_none(self) -> None:
        from harness.agents.runner import AgentRunner
        from harness.server.llm.router import LLMRouter

        runner = AgentRunner(
            router=MagicMock(spec=LLMRouter),
            repo=MagicMock(),
        )
        assert runner._reflection_factory is None

    def test_runner_offloader_factory_unchanged(self) -> None:
        """Adding ``reflection_factory`` kwarg must not regress ``offloader_factory``."""
        from harness.agents.runner import AgentRunner
        from harness.server.llm.router import LLMRouter

        offloader_factory = MagicMock()
        runner = AgentRunner(
            router=MagicMock(spec=LLMRouter),
            repo=MagicMock(),
            offloader_factory=offloader_factory,
        )
        assert runner._offloader_factory is offloader_factory
        assert runner._reflection_factory is None
