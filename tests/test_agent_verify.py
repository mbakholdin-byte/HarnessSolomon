"""Tests for harness.agents.verify (Phase 2.0, Step 6)."""
from __future__ import annotations

from typing import Any

import pytest

from harness.agents.verify import (
    AdversarialResult,
    AdversarialVerify,
    JudgeVote,
    _extract_verdict,
)
from harness.server.llm.router import CompletionResult


# === FakeRouter ===

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


# === _extract_verdict ===

@pytest.mark.parametrize(
    "text,expected",
    [
        ("VERDICT: PASS — looks correct", ("PASS", "looks correct")),
        ("VERDICT: FAIL — missing import", ("FAIL", "missing import")),
        ("PASS", ("PASS", "")),
        ("FAIL", ("FAIL", "")),
        # Multi-line reply with PASS on the second line: justification is
        # the line CONTAINING the verdict, with the verdict itself stripped.
        ("Reasoning...\nVERDICT: PASS — second line", ("PASS", "second line")),
        ("I think... PASS actually.", ("PASS", "actually.")),
        ("nothing useful here", ("FAIL", "nothing useful here")),
        ("", ("FAIL", "")),
    ],
)
def test_extract_verdict(text: str, expected: tuple[str, str]) -> None:
    assert _extract_verdict(text) == expected


# === Constructor validation ===

def test_adv_verify_judges_zero_raises() -> None:
    with pytest.raises(ValueError, match="judges must be >= 1"):
        AdversarialVerify(FakeRouter(), judges=0)


def test_adv_verify_judges_too_many_raises() -> None:
    with pytest.raises(ValueError, match="judges must be <= 5"):
        AdversarialVerify(FakeRouter(), judges=10)


def test_adv_verify_default_judges_from_settings() -> None:
    """Default judges count comes from settings.subagent_judges (default 2)."""
    adv = AdversarialVerify(FakeRouter())  # no judges=
    assert adv.judges == 2


def test_adv_verify_judges_one_allowed() -> None:
    """Single-judge is a valid degradation mode."""
    adv = AdversarialVerify(FakeRouter(), judges=1)
    assert adv.judges == 1


# === Voting: 2-judge panel ===

async def test_2judge_both_pass_accepted() -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: PASS — ok", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: PASS — fine", tool_calls=None, usage={}, cost=0.01),
    ])
    adv = AdversarialVerify(router, judges=2)  # type: ignore[arg-type]
    assert await adv.run("task", "answer") is True


async def test_2judge_both_fail_rejected() -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: FAIL — wrong", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: FAIL — nope", tool_calls=None, usage={}, cost=0.01),
    ])
    adv = AdversarialVerify(router, judges=2)  # type: ignore[arg-type]
    assert await adv.run("task", "answer") is False


async def test_2judge_split_rejected() -> None:
    """1 PASS + 1 FAIL on 2-judge panel = FAIL (unanimous for safety)."""
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: PASS — ok", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: FAIL — bad", tool_calls=None, usage={}, cost=0.01),
    ])
    adv = AdversarialVerify(router, judges=2)  # type: ignore[arg-type]
    assert await adv.run("task", "answer") is False


# === Voting: 3-judge panel ===

async def test_3judge_majority_pass_accepted() -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: FAIL", tool_calls=None, usage={}, cost=0.01),
    ])
    adv = AdversarialVerify(router, judges=3)  # type: ignore[arg-type]
    assert await adv.run("task", "answer") is True


async def test_3judge_majority_fail_rejected() -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: FAIL", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: FAIL", tool_calls=None, usage={}, cost=0.01),
    ])
    adv = AdversarialVerify(router, judges=3)  # type: ignore[arg-type]
    assert await adv.run("task", "answer") is False


# === Vote details ===

async def test_run_with_details_returns_full_breakdown() -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: PASS — good", tool_calls=None, usage={}, cost=0.01),
        CompletionResult(content="VERDICT: FAIL — missing", tool_calls=None, usage={}, cost=0.01),
    ])
    adv = AdversarialVerify(router, judges=2)  # type: ignore[arg-type]
    result = await adv.run_with_details("task", "answer")
    assert isinstance(result, AdversarialResult)
    assert result.passed is False  # 1-1 → fail (2-judge rule)
    assert len(result.votes) == 2
    assert result.votes[0].vote == "PASS"
    assert result.votes[1].vote == "FAIL"
    assert "good" in result.votes[0].justification
    assert "missing" in result.votes[1].justification
    assert result.total_cost == pytest.approx(0.02, abs=1e-6)


async def test_total_cost_sums_across_judges() -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.005),
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.007),
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.003),
    ])
    adv = AdversarialVerify(router, judges=3)  # type: ignore[arg-type]
    result = await adv.run_with_details("task", "answer")
    assert result.total_cost == pytest.approx(0.015, abs=1e-6)


# === Error handling: a judge errored → vote FAIL ===

async def test_judge_exception_counts_as_fail() -> None:
    """If one judge raises, the run completes and that vote is FAIL."""
    call_count = {"n": 0}

    class IntermittentRouter(FakeRouter):
        async def completion(self, **kwargs):
            self.calls.append(kwargs)
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("judge 1 timed out")
            return CompletionResult(
                content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.01,
            )

    adv = AdversarialVerify(IntermittentRouter(), judges=2)  # type: ignore[arg-type]
    # 1 FAIL (exception) + 1 PASS → 2-judge rule requires unanimous → fail
    result = await adv.run_with_details("task", "answer")
    assert result.passed is False
    # First vote: broken (error recorded, vote is FAIL).
    assert result.votes[0].vote == "FAIL"
    assert result.votes[0].error is not None
    assert "judge 1 timed out" in result.votes[0].error
    # Second vote: clean PASS.
    assert result.votes[1].vote == "PASS"
    assert result.votes[1].error is None


async def test_fallback_used_when_all_judges_error() -> None:
    class AllErrorRouter(FakeRouter):
        async def completion(self, **kwargs):
            raise RuntimeError("nope")

    adv = AdversarialVerify(AllErrorRouter(), judges=2)  # type: ignore[arg-type]
    result = await adv.run_with_details("task", "answer")
    assert result.fallback_used is True
    # All votes are FAIL, so 2-judge → not passed.
    assert result.passed is False


# === Golden: known-bad answer → judges say FAIL ===

async def test_golden_known_bad_answer_fail() -> None:
    """A response that claims the function works when it doesn't — FAIL."""
    prompt = (
        "Write a function `is_adult(age: int) -> bool` that returns True "
        "for age >= 18 and False otherwise."
    )
    # A WRONG answer: claims 21 threshold instead of 18.
    bad_answer = (
        "```python\n"
        "def is_adult(age: int) -> bool:\n"
        "    return age >= 21  # adult means 21 in the US\n"
        "```\n"
    )
    # Both judges correctly identify the discrepancy.
    router = FakeRouter(scripted=[
        CompletionResult(
            content="VERDICT: FAIL — function returns True only for age >= 21, not 18",
            tool_calls=None, usage={}, cost=0.0,
        ),
        CompletionResult(
            content="VERDICT: FAIL — threshold is wrong",
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    adv = AdversarialVerify(router, judges=2)  # type: ignore[arg-type]
    assert await adv.run(prompt, bad_answer) is False


async def test_golden_known_good_answer_pass() -> None:
    """A correct answer — judges say PASS."""
    prompt = (
        "Write a function `is_adult(age: int) -> bool` that returns True "
        "for age >= 18 and False otherwise."
    )
    good_answer = (
        "```python\n"
        "def is_adult(age: int) -> bool:\n"
        "    return age >= 18\n"
        "```\n"
    )
    router = FakeRouter(scripted=[
        CompletionResult(
            content="VERDICT: PASS — correct threshold and signature",
            tool_calls=None, usage={}, cost=0.0,
        ),
        CompletionResult(
            content="VERDICT: PASS — looks good",
            tool_calls=None, usage={}, cost=0.0,
        ),
    ])
    adv = AdversarialVerify(router, judges=2)  # type: ignore[arg-type]
    assert await adv.run(prompt, good_answer) is True


# === Temperature forwarding ===

async def test_temperature_forwarded_to_router() -> None:
    router = FakeRouter(scripted=[
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.0),
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.0),
    ])
    adv = AdversarialVerify(router, judges=2, temperature=0.7)  # type: ignore[arg-type]
    await adv.run("task", "answer")
    for call in router.calls:
        assert call["temperature"] == 0.7


# === JudgeVote schema ===

def test_judge_vote_schema() -> None:
    v = JudgeVote(vote="PASS", justification="looks good", cost=0.01)
    assert v.vote == "PASS"
    assert v.error is None


# === PASS/FAIL only — anything else is FAIL ===

async def test_garbage_verdict_counted_as_fail() -> None:
    """A judge that says 'MAYBE' is treated as FAIL (regex picks PASS/FAIL only)."""
    router = FakeRouter(scripted=[
        CompletionResult(content="MAYBE", tool_calls=None, usage={}, cost=0.0),
        CompletionResult(content="VERDICT: PASS", tool_calls=None, usage={}, cost=0.0),
    ])
    adv = AdversarialVerify(router, judges=2)  # type: ignore[arg-type]
    # 1 garbage (=FAIL) + 1 PASS = 1-1 on 2-judge = reject.
    assert await adv.run("task", "answer") is False
