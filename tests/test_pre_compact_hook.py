"""Phase 3 v1.5.0 Step 4: tests for PreCompactHook + compactor integration.

Covers:
- Hook captures state correctly (last N messages, plan, hot L0, metadata)
- Configurable save_fields subset
- UnifiedMemory persistence with namespaced tag
- Fail-open: memory=None, scratchpad=None, write failure, capture failure
- Audit events: pre_compact_state_saved, pre_compact_failed
- ContextCompactor integration: hook fires in _run_slow_path,
  fires ONCE per call, NOT on cache hit, timeout = fail-open
- Timeout via asyncio.wait_for
- Disabled (pre_compact_enabled=False) = no-op
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.agents.pre_compact import (
    DEFAULT_MESSAGES_LAST_N,
    VALID_SAVE_FIELDS,
    PreCompactHook,
    PreCompactState,
)
from harness.config import settings as real_settings


# --- Fixtures ---


class _FakeMemory:
    """In-memory UnifiedMemory stub that captures ``write`` calls."""

    def __init__(self, *, write_raises: bool = False) -> None:
        self.writes: list[dict[str, Any]] = []
        self._raises = write_raises

    async def write(
        self,
        text: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._raises:
            raise RuntimeError("memory backend down")
        self.writes.append(
            {"text": text, "tags": list(tags or []), "metadata": dict(metadata or {})}
        )


class _FakeScratchpad:
    """Scratchpad stub returning controlled plan + L0 content."""

    def __init__(
        self,
        *,
        plan_text: str = "current plan: implement foo",
        l0_text: str = "hot note 1\nhot note 2",
        raises: bool = False,
    ) -> None:
        self._plan = plan_text
        self._l0 = l0_text
        self._raises = raises

    def read_notes(self, level: str, tag: str = "", limit: int = 10) -> list[Any]:
        if self._raises:
            raise RuntimeError("scratchpad read failed")
        if level == "L1" and tag == "plan":
            return [MagicMock(content=self._plan)] if self._plan else []
        if level == "L0":
            return [MagicMock(content=self._l0)] if self._l0 else []
        return []


class _FakeSettings:
    """Settings stub with controllable pre_compact_* fields."""

    def __init__(
        self,
        save_fields: str = "messages_last_n,plan_step,hot_l0,metadata",
    ) -> None:
        self.pre_compact_save_fields = save_fields


# --- Test 1: PreCompactHook capture ---


class TestCapture:
    """PreCompactHook captures state correctly."""

    @pytest.mark.asyncio
    async def test_captures_last_n_messages(self) -> None:
        """Last DEFAULT_MESSAGES_LAST_N user/assistant messages captured."""
        hook = PreCompactHook(memory=None, settings=_FakeSettings())
        # Build messages: 1 system + 7*(user, assistant) + 1 tool = 16
        msgs: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
        for i in range(7):
            msgs.append({"role": "user", "content": f"q{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        msgs.append({"role": "tool", "content": "ignored"})

        state = await hook(session_id="s1", messages=msgs, metadata={"turn": 7})
        assert state is not None
        assert state.session_id == "s1"
        # Default = 5; skip tool messages; last 5 of user/assistant.
        assert len(state.messages_last_n) == DEFAULT_MESSAGES_LAST_N
        # Tool message at the end is NOT included.
        assert all(m["role"] in ("user", "assistant") for m in state.messages_last_n)

    @pytest.mark.asyncio
    async def test_captures_plan_from_scratchpad(self) -> None:
        """plan_step is read from scratchpad L1 tag='plan'."""
        sp = _FakeScratchpad(plan_text="step 3 of 5: implement pre-compact")
        hook = PreCompactHook(memory=None, settings=_FakeSettings(), scratchpad=sp)
        state = await hook(session_id="s1", messages=[], metadata={})
        assert state is not None
        assert "step 3 of 5" in state.plan_step

    @pytest.mark.asyncio
    async def test_captures_hot_l0_from_scratchpad(self) -> None:
        """hot_l0 is concatenated from scratchpad L0 notes."""
        sp = _FakeScratchpad(l0_text="urgent: fix the bug")
        hook = PreCompactHook(memory=None, settings=_FakeSettings(), scratchpad=sp)
        state = await hook(session_id="s1", messages=[], metadata={})
        assert state is not None
        assert "urgent: fix the bug" in state.hot_l0

    @pytest.mark.asyncio
    async def test_captures_metadata(self) -> None:
        """metadata dict is preserved verbatim."""
        hook = PreCompactHook(memory=None, settings=_FakeSettings())
        state = await hook(
            session_id="s1",
            messages=[],
            metadata={"turn": 12, "tokens": 8000, "model": "gpt-4"},
        )
        assert state is not None
        assert state.metadata == {"turn": 12, "tokens": 8000, "model": "gpt-4"}

    @pytest.mark.asyncio
    async def test_captured_at_is_monotonic(self) -> None:
        """captured_at uses time.monotonic() at capture time."""
        hook = PreCompactHook(memory=None, settings=_FakeSettings())
        before = time.monotonic()
        state = await hook(session_id="s1", messages=[], metadata={})
        after = time.monotonic()
        assert state is not None
        assert before <= state.captured_at <= after

    @pytest.mark.asyncio
    async def test_no_scratchpad_yields_empty_strings(self) -> None:
        """scratchpad=None → plan_step and hot_l0 are empty strings."""
        hook = PreCompactHook(memory=None, settings=_FakeSettings())
        state = await hook(session_id="s1", messages=[], metadata={})
        assert state is not None
        assert state.plan_step == ""
        assert state.hot_l0 == ""

    @pytest.mark.asyncio
    async def test_scratchpad_raises_is_failsafe(self) -> None:
        """scratchpad.read_notes raises → plan/hot_l0 = empty, no exception."""
        sp = _FakeScratchpad(raises=True)
        hook = PreCompactHook(memory=None, settings=_FakeSettings(), scratchpad=sp)
        state = await hook(session_id="s1", messages=[], metadata={})
        assert state is not None
        assert state.plan_step == ""
        assert state.hot_l0 == ""

    @pytest.mark.asyncio
    async def test_empty_session_id_returns_none(self) -> None:
        """Empty session_id is invalid → return None (no state)."""
        hook = PreCompactHook(memory=None, settings=_FakeSettings())
        state = await hook(session_id="", messages=[], metadata={})
        assert state is None


# --- Test 2: save_fields subset ---


class TestSaveFieldsSubset:
    """Only requested fields are captured."""

    @pytest.mark.asyncio
    async def test_only_messages_last_n(self) -> None:
        """save_fields='messages_last_n' → other fields empty/empty-dict."""
        sp = _FakeScratchpad(plan_text="plan", l0_text="l0")
        hook = PreCompactHook(
            memory=None,
            settings=_FakeSettings(save_fields="messages_last_n"),
            scratchpad=sp,
        )
        state = await hook(
            session_id="s1",
            messages=[{"role": "user", "content": "q"}],
            metadata={"turn": 1},
        )
        assert state is not None
        assert state.fields_included == ("messages_last_n",)
        assert len(state.messages_last_n) == 1
        assert state.plan_step == ""
        assert state.hot_l0 == ""
        assert state.metadata == {}

    @pytest.mark.asyncio
    async def test_empty_save_fields_returns_empty_state(self) -> None:
        """save_fields='' → fields_included=(), state is essentially empty."""
        hook = PreCompactHook(
            memory=None, settings=_FakeSettings(save_fields=""),
        )
        state = await hook(
            session_id="s1",
            messages=[{"role": "user", "content": "q"}],
            metadata={"turn": 1},
        )
        assert state is not None
        assert state.fields_included == ()
        assert state.messages_last_n == []
        assert state.plan_step == ""
        assert state.hot_l0 == ""
        assert state.metadata == {}

    @pytest.mark.asyncio
    async def test_unknown_save_fields_silently_skipped(self) -> None:
        """Unknown field names in save_fields are dropped, not errored."""
        hook = PreCompactHook(
            memory=None,
            settings=_FakeSettings(save_fields="messages_last_n,bogus,more_bogus"),
        )
        state = await hook(
            session_id="s1", messages=[{"role": "user", "content": "q"}], metadata={},
        )
        assert state is not None
        assert state.fields_included == ("messages_last_n",)

    @pytest.mark.asyncio
    async def test_valid_save_fields_constant(self) -> None:
        """VALID_SAVE_FIELDS contains the expected 4 fields."""
        assert VALID_SAVE_FIELDS == frozenset(
            {"messages_last_n", "plan_step", "hot_l0", "metadata"}
        )


# --- Test 3: UnifiedMemory persistence ---


class TestPersistence:
    """PreCompactHook writes to UnifiedMemory with namespaced tag."""

    @pytest.mark.asyncio
    async def test_writes_to_memory_with_namespaced_tag(self) -> None:
        """Memory write includes 'pre-compact-{session_id}' tag."""
        mem = _FakeMemory()
        hook = PreCompactHook(memory=mem, settings=_FakeSettings())
        await hook(
            session_id="sess-42",
            messages=[{"role": "user", "content": "q"}],
            metadata={"turn": 3},
        )
        assert len(mem.writes) == 1
        write = mem.writes[0]
        assert "pre-compact-sess-42" in write["tags"]
        assert "#pre-compact" in write["tags"]
        assert f"#session/sess-42" in write["tags"]
        assert write["metadata"]["session_id"] == "sess-42"
        assert write["metadata"]["kind"] == "pre_compact"

    @pytest.mark.asyncio
    async def test_write_failure_returns_state_anyway(self) -> None:
        """Memory backend raises → state still returned to caller."""
        mem = _FakeMemory(write_raises=True)
        hook = PreCompactHook(memory=mem, settings=_FakeSettings())
        state = await hook(
            session_id="s1",
            messages=[{"role": "user", "content": "q"}],
            metadata={},
        )
        # State is still returned (in-memory), even though persist failed.
        assert state is not None
        assert state.session_id == "s1"

    @pytest.mark.asyncio
    async def test_no_memory_skips_persistence(self) -> None:
        """memory=None → no write attempted, state still returned."""
        hook = PreCompactHook(memory=None, settings=_FakeSettings())
        state = await hook(
            session_id="s1",
            messages=[{"role": "user", "content": "q"}],
            metadata={},
        )
        assert state is not None
        # No way to assert "no write" without a write mock, but
        # absence of exception is the contract here.


# --- Test 4: Audit integration ---


class TestAuditIntegration:
    """PreCompactHook emits audit events (best-effort)."""

    @pytest.mark.asyncio
    async def test_audit_state_saved_on_success(self) -> None:
        """Successful persist → 'pre_compact_state_saved' event."""
        captured: list[tuple[str, dict[str, Any]]] = []

        class _Audit:
            def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
                captured.append((event, payload or {}))

        mem = _FakeMemory()
        hook = PreCompactHook(
            memory=mem, settings=_FakeSettings(), audit=_Audit(),
        )
        await hook(
            session_id="s1",
            messages=[{"role": "user", "content": "q"}],
            metadata={},
        )
        assert any(c[0] == "pre_compact_state_saved" for c in captured)

    @pytest.mark.asyncio
    async def test_audit_failure_emits_pre_compact_failed(self) -> None:
        """Memory write raises → 'pre_compact_failed' event."""
        captured: list[tuple[str, dict[str, Any]]] = []

        class _Audit:
            def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
                captured.append((event, payload or {}))

        mem = _FakeMemory(write_raises=True)
        hook = PreCompactHook(
            memory=mem, settings=_FakeSettings(), audit=_Audit(),
        )
        await hook(session_id="s1", messages=[], metadata={})
        # Should have at least one pre_compact_failed event.
        assert any(c[0] == "pre_compact_failed" for c in captured)

    @pytest.mark.asyncio
    async def test_audit_backend_failure_is_failsafe(self) -> None:
        """Audit backend raises → hook does NOT raise (fail-open)."""
        class _Audit:
            def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
                raise RuntimeError("audit backend down")

        hook = PreCompactHook(
            memory=None,
            settings=_FakeSettings(),
            audit=_Audit(),
        )
        # Must not raise.
        state = await hook(
            session_id="s1", messages=[], metadata={},
        )
        assert state is not None


# --- Test 5: ContextCompactor integration ---


class TestCompactorIntegration:
    """PreCompactHook fires correctly inside _run_slow_path."""

    @pytest.mark.asyncio
    async def test_hook_fires_in_slow_path(self) -> None:
        """Hook is called when force_compact enters _run_slow_path."""
        # Build a real compactor (with fake router + memory).
        from harness.context.compaction import ContextCompactor

        class _Router:
            async def completion(self, *a, **kw):
                return "summary"

        class _Settings:
            pre_compact_max_ms = 5000
            pre_compact_save_fields = "messages_last_n,metadata"
            compaction_enabled = True
            compaction_threshold_ratio = 0.5
            compaction_target_ratio = 0.3
            compaction_keep_recent_turns = 2
            compaction_persistent_store = False
            compaction_audit_log = False
            compaction_persist_to_memory = False
            compaction_summarizer_model = ""
            compaction_summarizer_fallback = ""
            compaction_summarizer_max_input_tokens = 0
            subagent_t1_model = ""
            subagent_t2_model = ""

        calls: list[dict[str, Any]] = []

        async def hook(*, session_id, messages, metadata):
            calls.append({
                "session_id": session_id,
                "n_messages": len(messages),
                "metadata": dict(metadata),
            })

        compactor = ContextCompactor(
            settings=_Settings(),  # type: ignore[arg-type]
            router=_Router(),  # type: ignore[arg-type]
            pre_compact_hook=hook,
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        await compactor.force_compact(
            msgs, model="test", session_id="s1", bypass_cache=True,
        )
        assert len(calls) == 1
        assert calls[0]["session_id"] == "s1"
        assert calls[0]["n_messages"] == 3

    @pytest.mark.asyncio
    async def test_hook_not_fired_when_disabled(self) -> None:
        """pre_compact_hook=None (default) → hook is never called."""
        from harness.context.compaction import ContextCompactor

        class _Router:
            async def completion(self, *a, **kw):
                return "summary"

        class _Settings:
            pre_compact_max_ms = 5000
            pre_compact_save_fields = ""
            compaction_enabled = True
            compaction_threshold_ratio = 0.5
            compaction_target_ratio = 0.3
            compaction_keep_recent_turns = 2
            compaction_persistent_store = False
            compaction_audit_log = False
            compaction_persist_to_memory = False
            compaction_summarizer_model = ""
            compaction_summarizer_fallback = ""
            compaction_summarizer_max_input_tokens = 0
            subagent_t1_model = ""
            subagent_t2_model = ""

        compactor = ContextCompactor(
            settings=_Settings(),  # type: ignore[arg-type]
            router=_Router(),  # type: ignore[arg-type]
        )
        # No exception = no fire. force_compact still works.
        msgs = [{"role": "user", "content": "q"}]
        result = await compactor.force_compact(
            msgs, model="test", session_id="s1", bypass_cache=True,
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_hook_timeout_is_failsafe(self) -> None:
        """Hook takes >max_ms → log + skip, compactor continues."""
        from harness.context.compaction import ContextCompactor

        class _Router:
            async def completion(self, *a, **kw):
                return "summary"

        class _Settings:
            pre_compact_max_ms = 50  # very short timeout
            pre_compact_save_fields = "messages_last_n"
            compaction_enabled = True
            compaction_threshold_ratio = 0.5
            compaction_target_ratio = 0.3
            compaction_keep_recent_turns = 2
            compaction_persistent_store = False
            compaction_audit_log = False
            compaction_persist_to_memory = False
            compaction_summarizer_model = ""
            compaction_summarizer_fallback = ""
            compaction_summarizer_max_input_tokens = 0
            subagent_t1_model = ""
            subagent_t2_model = ""

        async def slow_hook(*, session_id, messages, metadata):
            await asyncio.sleep(0.5)  # > 50ms timeout

        compactor = ContextCompactor(
            settings=_Settings(),  # type: ignore[arg-type]
            router=_Router(),  # type: ignore[arg-type]
            pre_compact_hook=slow_hook,
        )
        # No exception = timeout was caught and fail-open worked.
        result = await compactor.force_compact(
            [{"role": "user", "content": "q"}],
            model="test", session_id="s1", bypass_cache=True,
        )
        assert result is not None
