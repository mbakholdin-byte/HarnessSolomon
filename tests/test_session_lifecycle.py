"""Tests for ``harness.server.agent.lifecycle.SessionLifecycle`` (Phase 3 v1.4.0).

Covers:
  - lifecycle calls ``reflect()`` on ``__aexit__`` when reflection
    is enabled and events were collected
  - reflect failure is logged and swallowed (fail-open)
  - ``__aexit__`` is a no-op when ``reflection_enabled=False``
  - ``__aexit__`` is a no-op when ``runtime._reflection`` is None
  - ``__aexit__`` is a no-op when no events were collected
  - ``getattr(runtime, "_reflection", None)`` chain — works without
    a ``_reflection`` attribute on the runtime
  - per-call timeout via ``asyncio.wait_for``
  - audit event emitted on failure (timeout / generic exception)
  - ``ToolRuntime`` accepts the new ``reflection`` kwarg (mirror of
    ``tool_offloader``) and exposes it as ``runtime._reflection``

We use lightweight stub objects for the runtime, settings, and audit
— the lifecycle is a pure dispatcher and should not require a real
LLM router, scratchpad, or reflection module to be importable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.server.agent.lifecycle import SessionLifecycle


# ---------------------------------------------------------------------------
# Stubs / fakes
# ---------------------------------------------------------------------------


class FakeSettings:
    """Minimal settings object with the fields SessionLifecycle reads."""

    def __init__(
        self,
        *,
        reflection_enabled: bool = True,
        reflection_max_ms: int = 10_000,
    ) -> None:
        self.reflection_enabled = reflection_enabled
        self.reflection_max_ms = reflection_max_ms


class FakeRuntime:
    """Stub runtime. Pass ``reflection=`` to set ``_reflection``."""

    def __init__(self, *, reflection: Any = None) -> None:
        self._reflection = reflection


def make_reflection_mock(*, side_effect: BaseException | None = None) -> MagicMock:
    """Return a MagicMock whose ``reflect`` is an ``AsyncMock``.

    If ``side_effect`` is given, ``reflect`` raises it when awaited.
    """
    from unittest.mock import AsyncMock

    mock = MagicMock()
    reflect: AsyncMock = AsyncMock(side_effect=side_effect)
    if side_effect is None:
        reflect.return_value = ["lesson"]  # type: ignore[assignment]
    mock.reflect = reflect
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionLifecycleExit:
    async def test_calls_reflect_on_exit_with_events(self) -> None:
        """Happy path: enter, do work (collect events), exit → reflect() called."""
        reflection = make_reflection_mock()
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True, reflection_max_ms=5000)
        events: list[Any] = ["e1", "e2", "e3"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings,
        ):
            pass

        reflection.reflect.assert_awaited_once_with(events)

    async def test_skips_when_reflection_disabled(self) -> None:
        """``reflection_enabled=False`` → ``__aexit__`` is a no-op."""
        reflection = make_reflection_mock()
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=False)
        events: list[Any] = ["e1"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings,
        ):
            pass

        reflection.reflect.assert_not_awaited()

    async def test_skips_when_no_events(self) -> None:
        """Empty events list → reflect() not called (nothing to extract from)."""
        reflection = make_reflection_mock()
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True)
        events: list[Any] = []

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings,
        ):
            pass

        reflection.reflect.assert_not_awaited()

    async def test_skips_when_reflection_handle_none(self) -> None:
        """``runtime._reflection is None`` → no-op (backward compat with v1.3.x)."""
        runtime = FakeRuntime(reflection=None)
        settings = FakeSettings(reflection_enabled=True)
        events: list[Any] = ["e1", "e2"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings,
        ):
            pass

        # No exception, nothing called — we just verify exit was clean.

    async def test_uses_getattr_when_attribute_missing(self) -> None:
        """Runtime without ``_reflection`` attribute → handled via getattr default."""

        class _BareRuntime:
            pass

        runtime = _BareRuntime()
        settings = FakeSettings(reflection_enabled=True)
        events: list[Any] = ["e1"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings,
        ):
            pass

        # If we got here without AttributeError, getattr default worked.

    async def test_reflect_failure_is_swallowed(self, caplog) -> None:
        """Exception from reflect() → logged + swallowed (fail-open)."""
        reflection = make_reflection_mock(
            side_effect=RuntimeError("LLM is on fire"),
        )
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True)
        events: list[Any] = ["e1"]

        with caplog.at_level(logging.WARNING, logger="harness.server.agent.lifecycle"):
            async with SessionLifecycle(
                runtime=runtime, events=events, settings=settings,
            ):
                pass

        assert any(
            "reflection failed" in rec.message for rec in caplog.records
        ), "expected a 'reflection failed' warning, got: " + str(
            [r.message for r in caplog.records]
        )

    async def test_reflect_timeout_is_swallowed(self, caplog) -> None:
        """``asyncio.TimeoutError`` → logged + swallowed."""
        from unittest.mock import AsyncMock

        async def _slow(events: list[Any]) -> list[Any]:
            await asyncio.sleep(5)
            return []

        reflection = MagicMock()
        reflection.reflect = AsyncMock(side_effect=_slow)
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True, reflection_max_ms=50)
        events: list[Any] = ["e1"]

        with caplog.at_level(logging.WARNING, logger="harness.server.agent.lifecycle"):
            async with SessionLifecycle(
                runtime=runtime, events=events, settings=settings,
            ):
                pass

        assert any(
            "timed out" in rec.message for rec in caplog.records
        ), "expected a 'timed out' warning, got: " + str(
            [r.message for r in caplog.records]
        )

    async def test_audit_recorded_on_timeout(self) -> None:
        """Audit event ``reflection_timeout`` is recorded on asyncio.TimeoutError."""
        from unittest.mock import AsyncMock

        async def _slow(events: list[Any]) -> list[Any]:
            await asyncio.sleep(5)
            return []

        reflection = MagicMock()
        reflection.reflect = AsyncMock(side_effect=_slow)
        audit = MagicMock()
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True, reflection_max_ms=50)
        events: list[Any] = ["e1"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings, audit=audit,
        ):
            pass

        audit.record.assert_called_once()
        call_kwargs = audit.record.call_args.kwargs
        assert call_kwargs["event"] == "reflection_timeout"
        assert call_kwargs["max_ms"] == 50

    async def test_audit_recorded_on_generic_failure(self) -> None:
        """Audit event ``reflection_failed`` is recorded on generic exception."""
        reflection = make_reflection_mock(
            side_effect=ValueError("oops"),
        )
        audit = MagicMock()
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True)
        events: list[Any] = ["e1"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings, audit=audit,
        ):
            pass

        audit.record.assert_called_once()
        call_kwargs = audit.record.call_args.kwargs
        assert call_kwargs["event"] == "reflection_failed"
        assert "oops" in call_kwargs["error"]

    async def test_audit_failure_is_swallowed(self) -> None:
        """Audit ``record`` raising does NOT propagate."""
        reflection = make_reflection_mock(side_effect=ValueError("boom"))
        audit = MagicMock()
        audit.record.side_effect = RuntimeError("audit store is down")
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True)
        events: list[Any] = ["e1"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings, audit=audit,
        ):
            pass

        # If we got here without an exception escaping, audit failure
        # was swallowed.

    async def test_audit_none_is_handled(self) -> None:
        """``audit=None`` (default) does not raise on the failure path."""
        reflection = make_reflection_mock(side_effect=ValueError("boom"))
        runtime = FakeRuntime(reflection=reflection)
        settings = FakeSettings(reflection_enabled=True)
        events: list[Any] = ["e1"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings, audit=None,
        ):
            pass

        # If we got here, None audit was tolerated.

    async def test_per_call_timeout_passed_correctly(self) -> None:
        """Verify ``reflection_max_ms`` is converted to seconds and used in ``wait_for``."""
        reflection = make_reflection_mock()
        runtime = FakeRuntime(reflection=reflection)
        # reflection_max_ms = 1234 → timeout = 1.234 s
        settings = FakeSettings(reflection_enabled=True, reflection_max_ms=1234)
        events: list[Any] = ["e1"]

        # We can't introspect ``wait_for`` args from the public API, so
        # we verify the lifecycle does not raise and the call is made
        # within a sane bound (< 5 s).
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings,
        ):
            pass
        elapsed = loop.time() - t0
        assert elapsed < 5.0
        reflection.reflect.assert_awaited_once_with(events)

    async def test_default_max_ms_used_when_missing(self) -> None:
        """Settings without ``reflection_max_ms`` → use 10000 ms default."""
        reflection = make_reflection_mock()
        runtime = FakeRuntime(reflection=reflection)

        class _NoMaxMs:
            reflection_enabled = True

        settings = _NoMaxMs()
        events: list[Any] = ["e1"]

        async with SessionLifecycle(
            runtime=runtime, events=events, settings=settings,
        ):
            pass

        # No exception → default applied.
        reflection.reflect.assert_awaited_once_with(events)


class TestRuntimeReflectionKwarg:
    """The runtime must accept ``reflection`` as a kwarg (mirror ``tool_offloader``)."""

    def test_runtime_accepts_reflection_kwarg(self, tmp_path) -> None:
        from harness.server.agent.runtime import ToolRuntime

        reflection = MagicMock(name="reflection")
        runtime = ToolRuntime(
            project_root=tmp_path,
            reflection=reflection,
        )
        assert runtime._reflection is reflection

    def test_runtime_reflection_defaults_to_none(self, tmp_path) -> None:
        from harness.server.agent.runtime import ToolRuntime

        runtime = ToolRuntime(project_root=tmp_path)
        assert runtime._reflection is None

    def test_runtime_tool_offloader_unchanged(self, tmp_path) -> None:
        """Adding ``reflection`` kwarg must not regress ``tool_offloader``."""
        from harness.server.agent.runtime import ToolRuntime

        offloader = MagicMock(name="offloader")
        runtime = ToolRuntime(
            project_root=tmp_path,
            tool_offloader=offloader,
        )
        assert runtime._tool_offloader is offloader
        assert runtime._reflection is None  # default unchanged

    def test_runtime_accepts_both_kwargs(self, tmp_path) -> None:
        from harness.server.agent.runtime import ToolRuntime

        offloader = MagicMock(name="offloader")
        reflection = MagicMock(name="reflection")
        runtime = ToolRuntime(
            project_root=tmp_path,
            tool_offloader=offloader,
            reflection=reflection,
        )
        assert runtime._tool_offloader is offloader
        assert runtime._reflection is reflection
