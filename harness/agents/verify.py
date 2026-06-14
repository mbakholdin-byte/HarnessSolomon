"""Adversarial verification — 2/3 majority judges (Phase 2.0, Step 6).

For critical answers (e.g. a code agent claiming a fix is complete), we
run the SAME prompt through the SAME model ``N=2`` (or ``N=3``) times at
``temperature >= 0.4`` and require a majority of "PASS" verdicts.

The judges are not different models — we have one model — but sampling
variance + a structured "verdict" prompt acts as a cheap hallucination
filter. A real multi-model jury is Phase 2.1+ (T1 + T2 + T3 panels).
"""
from __future__ import annotations

import logging
from typing import Sequence

from pydantic import BaseModel, Field

from harness.config import settings
from harness.server.llm.router import LLMRouter

logger = logging.getLogger(__name__)


# === Constants ===

#: System prompt for the judge LLM. We use a binary PASS/FAIL format so
#: the verdict is robust to model verbosity.
JUDGE_SYSTEM_PROMPT: str = (
    "You are an adversarial verifier. Given a TASK and an ANSWER, "
    "decide whether the ANSWER correctly addresses the TASK.\n\n"
    "Reply with EXACTLY one line, in this format:\n"
    "  VERDICT: PASS — <one-sentence justification>\n"
    "  VERDICT: FAIL — <one-sentence justification>\n"
    "No other prose, no markdown."
)

#: Pulls the verdict token from a (possibly-noisy) judge reply.
_VERDICT_RE = __import__("re").compile(
    r"\b(VERDICT\s*:\s*)?\b(PASS|FAIL)\b",
    __import__("re").IGNORECASE,
)


# === Schema ===

class JudgeVote(BaseModel):
    """One judge's verdict."""

    vote: str  # "PASS" or "FAIL"
    justification: str = ""
    cost: float = 0.0
    error: str | None = None


class AdversarialResult(BaseModel):
    """Outcome of :meth:`AdversarialVerify.run`."""

    passed: bool
    votes: list[JudgeVote] = Field(default_factory=list)
    total_cost: float = 0.0
    fallback_used: bool = False  # True if all judges errored out


# === Verifier ===

class AdversarialVerify:
    """Run a panel of judges on an (prompt, answer) pair.

    Args:
        router:      An :class:`LLMRouter` to call for each judge.
        judges:      Number of judges (1–5). Default 2 (from settings).
        temperature: Sampling temperature for judges (default 0.4 — high
                     enough for variance, low enough to stay focused).
    """

    def __init__(
        self,
        router: LLMRouter,
        *,
        judges: int | None = None,
        temperature: float = 0.4,
    ) -> None:
        n = judges if judges is not None else settings.subagent_judges
        if n < 1:
            raise ValueError(f"judges must be >= 1, got {n}")
        if n > 5:
            raise ValueError(f"judges must be <= 5 (bounded cost), got {n}")
        self.router = router
        self.judges = n
        self.temperature = temperature

    async def run(
        self,
        prompt: str,
        answer: str,
        *,
        model: str | None = None,
    ) -> bool:
        """Run the panel and return True iff a majority vote PASSes.

        Semantics (matches the Phase 2 design):
          - ``judges=1``: single-shot, returns the one vote.
          - ``judges=2``: BOTH must PASS to accept (1-1 split → FAIL).
            This is the "2/3 majority" relaxation for even-sized panels.
          - ``judges=3``: ≥2 PASSes required.
          - ``judges=N`` (N > 3): majority (>= N/2 + 1) PASSes required.
        """
        result = await self.run_with_details(prompt, answer, model=model)
        return result.passed

    async def run_with_details(
        self,
        prompt: str,
        answer: str,
        *,
        model: str | None = None,
    ) -> AdversarialResult:
        """Same as :meth:`run` but also returns the per-judge breakdown.

        Useful for logging / debugging / golden tests.
        """
        used_model = model or settings.subagent_default_model
        messages: list[dict] = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"TASK:\n{prompt}\n\nANSWER:\n{answer}",
            },
        ]
        votes: list[JudgeVote] = []
        total_cost = 0.0
        for i in range(self.judges):
            try:
                response = await self.router.completion(
                    messages=messages, model=used_model,
                    temperature=self.temperature,
                )
                content = (response.content or "").strip()
                vote, just = _extract_verdict(content)
                total_cost += response.cost
                votes.append(JudgeVote(vote=vote, justification=just, cost=response.cost))
            except Exception as e:
                logger.warning("judge %d/%d failed: %s", i + 1, self.judges, e)
                # An errored judge counts as FAIL — we don't want a broken
                # judge to accidentally push the panel toward PASS.
                votes.append(JudgeVote(vote="FAIL", error=f"{type(e).__name__}: {e}"))

        pass_count = sum(1 for v in votes if v.vote == "PASS")
        fail_count = sum(1 for v in votes if v.vote == "FAIL")

        # Majority rule (with even-panel "unanimous" tightening for n=2).
        if self.judges == 2:
            passed = pass_count == 2  # both must PASS; 1-1 is FAIL
        else:
            passed = pass_count > fail_count

        return AdversarialResult(
            passed=passed,
            votes=votes,
            total_cost=total_cost,
            fallback_used=all(v.error is not None for v in votes),
        )


# === Helpers ===

def _extract_verdict(text: str) -> tuple[str, str]:
    """Return ``("PASS" | "FAIL", justification)`` from a judge reply.

    The first occurrence of PASS or FAIL wins. Anything else is FAIL.
    Justification is the rest of the line that contains the verdict.
    """
    m = _VERDICT_RE.search(text)
    if not m:
        return "FAIL", text[:200]
    raw = m.group(2).upper()
    # Find the line containing the match (so justification comes from THAT line).
    pos = 0
    for line in text.splitlines():
        line_end = pos + len(line)
        if pos <= m.start() < line_end:
            after = line[m.end() - pos:].lstrip(" —-:")
            return raw, after[:200] if after else ""
        pos = line_end + 1  # +1 for the newline
    # Fallback (single-line input without trailing newline).
    after = text[m.end():].lstrip(" —-:")
    return raw, after[:200] if after else ""
