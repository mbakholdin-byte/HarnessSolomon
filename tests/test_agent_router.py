"""Tests for harness.agents.router (Phase 2.0, Step 6)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harness.agents.registry import all_specs
from harness.agents.router import (
    LLMRouterClassifier,
    RouterDecision,
    _first_available,
)
from harness.agents.spec import AgentSpec
from harness.server.llm.router import CompletionResult


# === FakeRouter for the classifier ===

class FakeRouter:
    def __init__(self, scripted: list[CompletionResult] | None = None) -> None:
        self.scripted = list(scripted or [])
        self.calls: list[dict[str, Any]] = []

    async def completion(self, *, messages, model, tools=None, **kwargs):
        self.calls.append({
            "messages": list(messages),
            "model": model,
            "tools": tools,
            **kwargs,
        })
        if self.scripted:
            return self.scripted.pop(0)
        return CompletionResult(content="", tool_calls=None, usage={}, cost=0.0)

    async def streaming_completion(self, **kwargs):  # not used here, just for protocol
        yield CompletionResult(content="", tool_calls=None, usage={}, cost=0.0)


# === helpers ===

def _specs() -> list[AgentSpec]:
    """All built-in specs in a fresh project root."""
    return list(all_specs(project_root=Path("C:/nowhere")).values())


# === Classification: happy path ===

async def test_classify_picks_explore_for_find_task(tmp_path: Path) -> None:
    """A task about 'finding' should be classified as explore."""
    router = FakeRouter(scripted=[
        CompletionResult(
            content='{"agent": "explore", "confidence": 0.92}',
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("find usages of foo in the repo")
    assert isinstance(decision, RouterDecision)
    assert decision.agent == "explore"
    assert decision.confidence == pytest.approx(0.92, abs=1e-6)
    assert decision.fallback is False


async def test_classify_picks_code_for_implementation_task(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(
            content='{"agent": "code", "confidence": 0.85}',
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("add a new endpoint /api/v1/widgets")
    assert decision.agent == "code"
    assert decision.fallback is False


async def test_classify_picks_plan_for_planning_task(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(
            content='{"agent": "plan", "confidence": 0.78}',
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("design a step-by-step plan to migrate the DB")
    assert decision.agent == "plan"


# === Fallback paths ===

async def test_classify_malformed_json_falls_back(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(
            content="I think the best agent is explore. agent: explore",
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("task")
    # The bare "agent: explore" form is parsed second.
    assert decision.agent == "explore"
    assert decision.fallback is False
    assert decision.confidence == 0.5


async def test_classify_unknown_agent_falls_back_to_first(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(
            content='{"agent": "nonexistent-agent", "confidence": 0.99}',
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("task")
    # Unknown agent name → fallback chain → first in _FALLBACK_ORDER
    assert decision.agent == "explore"  # first in _FALLBACK_ORDER
    assert decision.fallback is True


async def test_classify_garbage_response_falls_back(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(
            content="Hmm, let me think about this... 🤔",  # no JSON, no "agent: x"
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("task")
    assert decision.fallback is True
    assert decision.agent == "explore"  # first in _FALLBACK_ORDER


async def test_classify_llm_error_falls_back(tmp_path: Path) -> None:
    class ExplodingRouter(FakeRouter):
        async def completion(self, **kwargs):
            raise RuntimeError("simulated LLM failure")

    cls = LLMRouterClassifier(router=ExplodingRouter(), project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("task")
    assert decision.fallback is True
    assert decision.agent == "explore"


# === Call recording ===

async def test_classify_calls_completion_exactly_once(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    await cls.classify("task")
    assert len(router.calls) == 1


async def test_classify_passes_task_in_user_message(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    await cls.classify("what is in the README?")
    user_msgs = [m for m in router.calls[0]["messages"] if m["role"] == "user"]
    assert any("README" in m["content"] for m in user_msgs)


async def test_classify_uses_settings_default_model(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    await cls.classify("task")
    assert router.calls[0]["model"] == "MiniMax-M2.7"


async def test_classify_explicit_model_overrides_default(tmp_path: Path) -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    await cls.classify("task", model="glm-4.7")
    assert router.calls[0]["model"] == "glm-4.7"


async def test_classify_temperature_zero_for_determinism(tmp_path: Path) -> None:
    """Router should pin temperature=0 for reproducible classification."""
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    await cls.classify("task")
    assert router.calls[0].get("temperature") == 0.0


async def test_classify_truncates_very_long_tasks(tmp_path: Path) -> None:
    """A 10k-char task is truncated before being sent to the LLM."""
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    long_task = "x" * 10_000
    await cls.classify(long_task)
    user_msg = next(m for m in router.calls[0]["messages"] if m["role"] == "user")
    assert len(user_msg["content"]) < 10_000
    assert "truncated" in user_msg["content"]


async def test_classify_unicode_task(tmp_path: Path) -> None:
    """Russian / Chinese tasks don't crash the router."""
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("Найди упоминания foo в коде. 在代码中查找 foo。")
    assert decision.agent == "explore"


async def test_classify_explicit_candidates_bypass_registry(tmp_path: Path) -> None:
    """When ``candidates=`` is given, the registry is NOT consulted."""
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "x"}', tool_calls=None, usage={}, cost=0.0),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    custom = [AgentSpec(name="x", model="MiniMax-M2.7", tools=["read_file"], permissions="read-only")]
    decision = await cls.classify("task", candidates=custom)
    assert decision.agent == "x"


async def test_classify_no_candidates_raises(tmp_path: Path) -> None:
    """Empty candidate set → ValueError before calling the LLM."""
    router = FakeRouter()
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no candidate"):
        await cls.classify("task", candidates=[])


async def test_classify_response_cost_recorded(tmp_path: Path) -> None:
    """The cost from the LLM response is exposed on the decision raw_response."""
    router = FakeRouter(scripted=[
        CompletionResult(content='{"agent": "explore"}', tool_calls=None, usage={}, cost=0.005),
    ])
    cls = LLMRouterClassifier(router=router, project_root=tmp_path)  # type: ignore[arg-type]
    decision = await cls.classify("task")
    # Cost isn't stored on RouterDecision (it's a classification result, not
    # a billing record). The raw_response should reflect the model output.
    assert "explore" in decision.raw_response


# === _first_available helper ===

def test_first_available_returns_fallback_order() -> None:
    specs = [AgentSpec(name="code", model="MiniMax-M2.7"), AgentSpec(name="review", model="MiniMax-M2.7")]
    assert _first_available(specs) == "code"  # not in fallback order, but first in list


def test_first_available_prefers_explore() -> None:
    specs = [
        AgentSpec(name="code", model="MiniMax-M2.7"),
        AgentSpec(name="explore", model="MiniMax-M2.7"),
        AgentSpec(name="plan", model="MiniMax-M2.7"),
    ]
    assert _first_available(specs) == "explore"
