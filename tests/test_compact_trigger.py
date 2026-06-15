"""Tests for ``harness.server.agent.compact_trigger.CompactTrigger`` (Phase 3 v1.4.0).

Covers:
  - happy path: compactor returns CompactResult → result returned + audited
  - compactor None → None + ``compact_unavailable`` audit
  - compactor raises → None + ``compact_failed`` audit
  - compactor takes too long → None + ``compact_timeout`` audit
  - per-call timeout uses ``manual_compact_max_ms`` from settings
  - ``bypass_cache`` is forwarded to the compactor
  - audit None is tolerated
  - audit raises is swallowed
  - ``manual_compact`` audit includes token savings + cache_hit
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from harness.server.agent.compact_trigger import CompactTrigger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeCompactResult:
    """Mimics :class:`harness.context.compaction.CompactResult`."""

    def __init__(
        self,
        *,
        original_tokens: int = 1000,
        compacted_tokens: int = 200,
        summary_preview: str = "...",
        cache_hit: bool = False,
    ) -> None:
        self.original_tokens = original_tokens
        self.compacted_tokens = compacted_tokens
        self.summary_preview = summary_preview
        self.cache_hit = cache_hit

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.compacted_tokens)


def make_settings(*, manual_compact_max_ms: int = 30_000) -> Any:
    return MagicMock(manual_compact_max_ms=manual_compact_max_ms)


def make_compactor(*, return_value: Any = None, side_effect: BaseException | None = None) -> MagicMock:
    """Stub compactor with ``force_compact`` async method."""
    compactor = MagicMock()
    if side_effect is not None:
        compactor.force_compact = AsyncMock(side_effect=side_effect)
    else:
        compactor.force_compact = AsyncMock(return_value=return_value)
    return compactor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCompactTriggerHappyPath:
    async def test_returns_compact_result_on_success(self) -> None:
        result = FakeCompactResult(original_tokens=1000, compacted_tokens=200, cache_hit=False)
        compactor = make_compactor(return_value=result)
        trigger = CompactTrigger(compactor, make_settings())
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is result
        compactor.force_compact.assert_awaited_once()

    async def test_audit_records_manual_compact_with_savings(self) -> None:
        result = FakeCompactResult(
            original_tokens=1000, compacted_tokens=200, cache_hit=True,
        )
        compactor = make_compactor(return_value=result)
        audit = MagicMock()
        trigger = CompactTrigger(compactor, make_settings(), audit=audit)
        await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        audit.record.assert_called_once()
        kw = audit.record.call_args.kwargs
        assert kw["event"] == "manual_compact"
        assert kw["original_tokens"] == 1000
        assert kw["compacted_tokens"] == 200
        assert kw["saved_tokens"] == 800
        assert kw["cache_hit"] is True
        assert kw["session_id"] == "s"

    async def test_bypass_cache_forwarded(self) -> None:
        result = FakeCompactResult()
        compactor = make_compactor(return_value=result)
        trigger = CompactTrigger(compactor, make_settings())
        await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m",
            session_id="s", bypass_cache=True,
        )
        # First positional arg is ``messages``, second is ``model``.
        call = compactor.force_compact.call_args
        assert call.args[0] == [{"role": "user", "content": "x"}]
        assert call.args[1] == "m"
        assert call.kwargs["bypass_cache"] is True
        assert call.kwargs["session_id"] == "s"

    async def test_bypass_cache_defaults_false(self) -> None:
        result = FakeCompactResult()
        compactor = make_compactor(return_value=result)
        trigger = CompactTrigger(compactor, make_settings())
        await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        call = compactor.force_compact.call_args
        assert call.kwargs["bypass_cache"] is False


class TestCompactTriggerFailureModes:
    async def test_compactor_none_returns_none(self) -> None:
        trigger = CompactTrigger(None, make_settings())
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is None

    async def test_compactor_none_audits_unavailable(self) -> None:
        audit = MagicMock()
        trigger = CompactTrigger(None, make_settings(), audit=audit)
        await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        kw = audit.record.call_args.kwargs
        assert kw["event"] == "compact_unavailable"
        assert kw["session_id"] == "s"

    async def test_compactor_raises_returns_none_and_audits(self) -> None:
        compactor = make_compactor(side_effect=ValueError("boom"))
        audit = MagicMock()
        trigger = CompactTrigger(compactor, make_settings(), audit=audit)
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is None
        kw = audit.record.call_args.kwargs
        assert kw["event"] == "compact_failed"
        assert "boom" in kw["error"]
        assert kw["session_id"] == "s"

    async def test_compactor_timeout_returns_none_and_audits(self) -> None:
        """force_compact that blocks for >max_ms → TimeoutError → audit timeout."""
        # AsyncMock that, on each call, raises asyncio.TimeoutError. The
        # real-world cause is asyncio.wait_for in the trigger firing —
        # but the trigger catches that TimeoutError and audits. We
        # simulate the post-wait_for exception by having the compactor
        # raise TimeoutError directly.
        compactor = MagicMock()
        compactor.force_compact = AsyncMock(side_effect=asyncio.TimeoutError())
        audit = MagicMock()
        trigger = CompactTrigger(
            compactor, make_settings(manual_compact_max_ms=50), audit=audit,
        )
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is None
        kw = audit.record.call_args.kwargs
        assert kw["event"] == "compact_timeout"
        assert kw["max_ms"] == 50
        assert kw["session_id"] == "s"

    async def test_audit_none_handled_on_failure_path(self) -> None:
        compactor = make_compactor(side_effect=ValueError("boom"))
        trigger = CompactTrigger(compactor, make_settings(), audit=None)
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is None

    async def test_audit_record_raises_is_swallowed(self) -> None:
        compactor = make_compactor(side_effect=ValueError("boom"))
        audit = MagicMock()
        audit.record.side_effect = RuntimeError("audit is down")
        trigger = CompactTrigger(compactor, make_settings(), audit=audit)
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is None


class TestCompactTriggerDefaults:
    async def test_default_max_ms_when_settings_missing(self) -> None:
        """Settings without ``manual_compact_max_ms`` → use 30_000 ms default."""
        result = FakeCompactResult()
        compactor = make_compactor(return_value=result)

        class _NoMaxMs:
            pass

        trigger = CompactTrigger(compactor, _NoMaxMs())
        # Should not raise; per-call timeout is 30 s default.
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is result

    async def test_max_ms_zero_treated_as_default(self) -> None:
        """``manual_compact_max_ms=0`` → fall back to 30_000 (defensive)."""
        result = FakeCompactResult()
        compactor = make_compactor(return_value=result)
        trigger = CompactTrigger(
            compactor, make_settings(manual_compact_max_ms=0),
        )
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        assert out is result

    async def test_audit_disabled_does_not_emit_manual_compact_event(self) -> None:
        """audit=None → no ``manual_compact`` event on success path."""
        result = FakeCompactResult()
        compactor = make_compactor(return_value=result)
        trigger = CompactTrigger(compactor, make_settings(), audit=None)
        out = await trigger.compact_now(
            [{"role": "user", "content": "x"}], model="m", session_id="s",
        )
        # No way to verify "no audit" without side effects — at minimum
        # the call returned successfully and did not raise.
        assert out is result
