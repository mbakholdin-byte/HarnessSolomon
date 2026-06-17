"""Phase 4.5 v1.15.0: block/modify semantics for OnRoutingDecision + OnCompaction.

Verifies that the production call sites honour the aggregate decision
returned by the hooks framework:

    OnRoutingDecision:
      - ``block``  → classifier returns a fallback ``RouterDecision``
        (first available candidate).
      - ``modify`` → classifier overrides ``decision.agent`` from the
        ``chosen_agent`` key in the modify payload.
      - ``allow``  → classifier returns the original LLM decision.

    OnCompaction:
      - ``block``  → compactor drops the summary and returns the
        sliding-window-only result (no data loss — recent tail is
        preserved).
      - ``allow``  → compactor returns the compacted-with-summary list.

The tests wire a fresh ``HookRunner`` into the global handle so
production code paths (which call ``safe_fire`` / the runner directly)
see the registered block/modify hooks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest

from harness.agents.router import LLMRouterClassifier, RouterDecision
from harness.agents.spec import AgentSpec
from harness.config import Settings
from harness.context.compaction import ContextCompactor
from harness.hooks.context import HookContext, HookDecision
from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec, reset_registry
from harness.hooks.runner import (
    HookRunner,
    set_global_hook_runner,
)
from harness.server.llm.router import CompletionResult


# === Shared fixtures ====================================================


@pytest.fixture
def fresh_runner() -> Iterator[HookRunner]:
    """Bind a clean HookRunner to the global handle for the test."""
    registry = HookRegistry()
    runner = HookRunner(registry, default_timeout_ms=500)
    set_global_hook_runner(runner)
    yield runner
    set_global_hook_runner(None)
    reset_registry()


@pytest.fixture(autouse=True)
def _reset_global_runner() -> Iterator[None]:
    """Ensure no leftover global runner leaks between tests."""
    set_global_hook_runner(None)
    reset_registry()
    yield
    set_global_hook_runner(None)
    reset_registry()


# === Router helpers =====================================================


class _FakeRouter:
    """Minimal LLM router stub that returns a scripted response."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def completion(self, *, messages, model, **kwargs):
        return CompletionResult(
            content=self._content, tool_calls=None, usage={}, cost=0.0,
        )

    async def streaming_completion(self, **kwargs):  # pragma: no cover
        yield CompletionResult(content="", tool_calls=None, usage={}, cost=0.0)


def _candidate_specs() -> list[AgentSpec]:
    """A small candidate set where 'explore' is the first-available fallback."""
    return [
        AgentSpec(
            name="explore", model="MiniMax-M2.7", tools=["read_file"],
            permissions="read-only", system_prompt="Read-only explorer.",
        ),
        AgentSpec(
            name="code", model="MiniMax-M2.7", tools=["read_file", "write_file"],
            permissions="full", system_prompt="Code agent.",
        ),
    ]


async def _register_decision_hook(
    runner: HookRunner, decision: str, *, override_agent: str = "",
) -> None:
    """Register an OnRoutingDecision hook returning ``decision``.

    For ``modify`` the hook injects ``chosen_agent=override_agent``
    into its output payload so the classifier can pick it up.
    """
    async def _hook(ctx: HookContext) -> HookDecision:
        if decision == "modify":
            return HookDecision(
                decision="modify",
                hook_id="test.routing.modify",
                output={"payload": {**ctx.payload, "chosen_agent": override_agent}},
            )
        return HookDecision(decision=decision, hook_id=f"test.routing.{decision}")

    await runner._registry.register(  # noqa: SLF001 — test-only
        HookSpec(
            hook_id=f"test.routing.{decision}",
            event=EventType.ON_ROUTING_DECISION,
            transport="builtin",
            callable=_hook,
        )
    )


# === A. OnRoutingDecision block-respecting =============================


async def test_routing_block_falls_back_to_first_available(
    fresh_runner: HookRunner, tmp_path: Path,
) -> None:
    """``block`` → classifier returns the first-available candidate.

    The LLM picks ``code``; the hook blocks; the classifier must
    return ``explore`` (first in ``_FALLBACK_ORDER`` present in the
    candidate set) with ``fallback=True``.
    """
    await _register_decision_hook(fresh_runner, "block")
    router = _FakeRouter('{"agent": "code", "confidence": 0.9}')
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("do something", candidates=_candidate_specs())
    assert isinstance(decision, RouterDecision)
    assert decision.agent == "explore", (
        f"block should fall back to explore, got {decision.agent!r}"
    )
    assert decision.fallback is True


async def test_routing_modify_overrides_chosen_agent(
    fresh_runner: HookRunner, tmp_path: Path,
) -> None:
    """``modify`` → classifier picks the agent from the modify payload.

    The LLM picks ``explore``; the hook modifies the payload to set
    ``chosen_agent=code``; the classifier must return ``code``.
    """
    await _register_decision_hook(fresh_runner, "modify", override_agent="code")
    router = _FakeRouter('{"agent": "explore", "confidence": 0.8}')
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("do something", candidates=_candidate_specs())
    assert isinstance(decision, RouterDecision)
    assert decision.agent == "code", (
        f"modify should override to code, got {decision.agent!r}"
    )


async def test_routing_allow_keeps_original(
    fresh_runner: HookRunner, tmp_path: Path,
) -> None:
    """``allow`` → classifier returns the original LLM decision unchanged."""
    await _register_decision_hook(fresh_runner, "allow")
    router = _FakeRouter('{"agent": "code", "confidence": 0.77}')
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("do something", candidates=_candidate_specs())
    assert isinstance(decision, RouterDecision)
    assert decision.agent == "code"
    assert decision.confidence == pytest.approx(0.77, abs=1e-6)
    assert decision.fallback is False


# === B. OnCompaction block-respecting ==================================


class _StubSummariser:
    """Stub LLM router that always returns the same summary text."""

    def __init__(self, summary: str = "SUMMARY OF DROPPED TURNS") -> None:
        self._summary = summary

    async def completion(self, *, messages, model, **kwargs):
        return CompletionResult(
            content=self._summary, tool_calls=None, usage={}, cost=0.0,
        )


def _build_messages(*, over_threshold: bool = True) -> list[dict[str, Any]]:
    """Build a message list large enough to trigger compaction.

    We target ``qwen3:8b`` (ctx=32768, threshold=0.75*32768≈24576,
    target=0.5*32768≈16384). With 30 turns of ~4000 chars each
    (≈1000 tokens), the total is ~60000 tokens — well over the
    threshold. The sliding window drops most of them, keeping the
    system message + the last ``keep_recent_turns`` (6) messages +
    any tool-call pairs. The dropped region (24 messages ≈ 24000
    tokens) is then summarised.
    """
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]
    big = "x" * 4000
    for i in range(30):
        msgs.append({"role": "user", "content": f"turn-{i}-{big}"})
        msgs.append({"role": "assistant", "content": f"reply-{i}-{big}"})
    # Recent tail (protected by keep_recent_turns).
    msgs.append({"role": "user", "content": "RECENT-TAIL-MARKER"})
    msgs.append({"role": "assistant", "content": "recent-reply"})
    return msgs


#: Model id with a small enough context window that the test message
#: list triggers the slow path. ``qwen3:8b`` has ctx=32768.
_COMPACT_MODEL = "qwen3:8b"


def _make_compactor(
    settings: Settings,
    summariser: _StubSummariser,
    *,
    session_id: str = "test-session",
) -> ContextCompactor:
    return ContextCompactor(
        settings, summariser,  # type: ignore[arg-type]
        memory=None, store=None, audit=None,
        session_id=session_id,
    )


@pytest.fixture
def compact_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings tuned so the test message list triggers summarisation.

    We shrink ``compaction_target_ratio`` and
    ``compaction_threshold_ratio`` so the sliding window can't
    bring the list under target on its own — forcing the
    summariser to run and inject a summary. The model context
    (qwen3:8b = 32768) is fixed; we only adjust the ratios.
    """
    s = Settings()
    # threshold = 0.05 * 32768 = 1638 tokens. The 30-turn list is
    # ~60000 tokens, so the threshold is comfortably exceeded.
    monkeypatch.setattr(s, "compaction_threshold_ratio", 0.05)
    # target = 0.02 * 32768 = 655 tokens. The sliding window keeps
    # system + 6 tail messages ≈ 6000 tokens, which is OVER target,
    # forcing the summariser to run on the dropped region.
    monkeypatch.setattr(s, "compaction_target_ratio", 0.02)
    return s


async def _register_compaction_hook(runner: HookRunner, decision: str) -> None:
    """Register an OnCompaction hook returning ``decision``."""
    async def _hook(ctx: HookContext) -> HookDecision:
        return HookDecision(decision=decision, hook_id=f"test.compaction.{decision}")

    await runner._registry.register(  # noqa: SLF001
        HookSpec(
            hook_id=f"test.compaction.{decision}",
            event=EventType.ON_COMPACTION,
            transport="builtin",
            callable=_hook,
        )
    )


def _has_summary(messages: list[dict[str, Any]]) -> bool:
    """True if any message carries the compaction-summary marker."""
    for m in messages:
        content = m.get("content") or ""
        if "[Compaction summary" in content or "[Conversation summary]" in content:
            return True
    return False


async def test_compaction_block_drops_summary(
    fresh_runner: HookRunner, compact_settings: Settings, tmp_path: Path,
) -> None:
    """``block`` → the returned list has NO summary message.

    The compactor computed a summary (slow path ran), but the hook
    blocked it. The output must be the sliding-window-only result.
    """
    await _register_compaction_hook(fresh_runner, "block")
    settings = compact_settings
    compactor = _make_compactor(settings, _StubSummariser())
    messages = _build_messages()
    result = await compactor.maybe_compact(messages, model=_COMPACT_MODEL)
    assert not _has_summary(result), (
        "block must drop the summary; found summary marker in result"
    )


async def test_compaction_block_keeps_sliding_window(
    fresh_runner: HookRunner, compact_settings: Settings, tmp_path: Path,
) -> None:
    """``block`` → the recent tail is still present (no data loss).

    The sliding-window result preserves the last
    ``keep_recent_turns`` messages. We assert the protected tail
    (``RECENT-TAIL-MARKER``) survives the block.
    """
    await _register_compaction_hook(fresh_runner, "block")
    settings = compact_settings
    compactor = _make_compactor(settings, _StubSummariser())
    messages = _build_messages()
    result = await compactor.maybe_compact(messages, model=_COMPACT_MODEL)
    # The recent tail must be present.
    tail_contents = [str(m.get("content", "")) for m in result]
    assert any("RECENT-TAIL-MARKER" in c for c in tail_contents), (
        "block must preserve the recent tail; RECENT-TAIL-MARKER missing"
    )
    # And no summary marker.
    assert not _has_summary(result)


async def test_compaction_allow_keeps_summary(
    fresh_runner: HookRunner, compact_settings: Settings, tmp_path: Path,
) -> None:
    """``allow`` → the returned list HAS the summary message."""
    await _register_compaction_hook(fresh_runner, "allow")
    settings = compact_settings
    compactor = _make_compactor(settings, _StubSummariser())
    messages = _build_messages()
    result = await compactor.maybe_compact(messages, model=_COMPACT_MODEL)
    assert _has_summary(result), (
        "allow must keep the summary; no summary marker found"
    )
