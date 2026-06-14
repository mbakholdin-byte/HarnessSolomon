"""Cost-aware T1→T2→T3 cascade (Phase 2.1, Step 1).

The :class:`LLMRouterClassifier` (Phase 2.0) returns a
:class:`~harness.agents.router.RouterDecision` with a ``confidence``
float. This module closes the loop: it maps that confidence to a
**model tier** (T1 cheap local, T2 cloud mid, T3 cloud premium) using
two thresholds from :class:`harness.config.Settings`.

The mapper is a **pure function** — no LLM calls, no I/O, no state.
It can be unit-tested with confidence values alone, and re-used by
the main agent loop, the merge queue, the CLI, or any other layer
that needs a tier decision.

Tier semantics (see ``docs/MODEL_REGISTRY.md``):

  - **T1 (Haiku-class)**: 8-12B local (Qwen3 8B, Gemma 4 12B), ``$0``
    per token. Default: ``qwen3:8b`` (Ollama).
  - **T2 (Sonnet-class)**: 30-70B cloud (Qwen3-Coder 30B A3B, GLM-4.7).
    Default: ``glm-4.7`` (ZhipuAI).
  - **T3 (Opus-class)**: frontier cloud (MiniMax M2.7, Kimi K2.6).
    Default: ``settings.subagent_default_model`` (Phase 2.0 default
    ``MiniMax-M2.7``).

Decision rule (``fallback=False``)::

    confidence >= high_threshold          → T1
    high_threshold > confidence >= low    → T2
    confidence < low                      → T3

When ``RouterDecision.fallback`` is ``True`` (the LLM gave us an
unparseable reply and we used the soft fallback chain) we **force T3**:
the router said "I don't know", so we don't take the cheap-local risk.

When ``t1_model`` is empty / None (e.g. CI without Ollama), T1 is
**skipped** and the cascade degrades to T2/T3 on the same thresholds.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from harness.config import settings


# === Constants ===

#: Tier names. Order is the cascade priority (cheap → expensive).
TIER_T1: str = "T1"
TIER_T2: str = "T2"
TIER_T3: str = "T3"
TIER_PRIORITY: tuple[str, ...] = (TIER_T1, TIER_T2, TIER_T3)

#: Sentinel string for "Tier-1 disabled" (e.g. CI without Ollama).
T1_DISABLED: str = ""


# === Schema ===

class CascadeDecision(BaseModel):
    """The output of :class:`TierSelector.select_tier`.

    Immutable. ``chosen_model`` is the model id to pass to
    :meth:`~harness.server.llm.router.LLRouter.completion`. ``tier``
    is one of ``T1``/``T2``/``T3``. ``reason`` is a short
    human-readable explanation suitable for logging — it does NOT
    affect the decision.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    chosen_model: str = Field(min_length=1)
    tier: str = Field(pattern=r"^T[123]$")
    reason: str = Field(min_length=1)


# === Selector ===

class TierSelector:
    """Map a router confidence to a tier + model id.

    Stateless. Re-construct freely or reuse a single instance — it
    holds no state across calls. The thresholds and model ids are
    captured at construction time so unit tests can pin them
    without monkeypatching ``settings``.
    """

    def __init__(
        self,
        *,
        t1_model: str | None = None,
        t2_model: str | None = None,
        t3_model: str | None = None,
        confidence_high: float | None = None,
        confidence_low: float | None = None,
    ) -> None:
        # Defaults pulled from settings only when the caller didn't
        # pin them — this lets tests construct a selector with
        # custom thresholds without monkeypatching global state.
        self.t1_model: str = (
            t1_model if t1_model is not None else settings.subagent_t1_model
        )
        self.t2_model: str = (
            t2_model if t2_model is not None else settings.subagent_t2_model
        )
        self.t3_model: str = (
            t3_model if t3_model is not None else settings.subagent_default_model
        )
        self.confidence_high: float = (
            confidence_high
            if confidence_high is not None
            else settings.subagent_confidence_high
        )
        self.confidence_low: float = (
            confidence_low
            if confidence_low is not None
            else settings.subagent_confidence_low
        )
        # Same guard as the Settings model_validator. We re-check
        # here so a manually-constructed selector (e.g. in a test)
        # can't bypass the validation.
        if self.confidence_low >= self.confidence_high:
            raise ValueError(
                f"confidence_low ({self.confidence_low}) must be < "
                f"confidence_high ({self.confidence_high})"
            )

    def select_tier(
        self, confidence: float, *, fallback: bool = False
    ) -> CascadeDecision:
        """Decide which tier to use for the given confidence.

        Args:
            confidence: The router's self-reported confidence in
                [0.0, 1.0]. Out-of-range values are clamped.
            fallback:   ``True`` if the router fell back to a default
                agent (e.g. parse failure). Forces T3 — cheap models
                aren't worth the risk when the router itself wasn't
                sure.

        Returns:
            A frozen :class:`CascadeDecision` carrying the chosen
            model id, tier name, and a one-line ``reason`` for logs.
        """
        # Defensive clamp. The router may report 1.001 from a chatty
        # model; we treat anything above 1.0 as a perfect 1.0.
        conf = max(0.0, min(1.0, float(confidence)))

        if fallback:
            return CascadeDecision(
                chosen_model=self.t3_model,
                tier=TIER_T3,
                reason=f"router fallback — forced T3 ({self.t3_model})",
            )

        if conf >= self.confidence_high:
            if self.t1_model and self.t1_model != T1_DISABLED:
                return CascadeDecision(
                    chosen_model=self.t1_model,
                    tier=TIER_T1,
                    reason=(
                        f"confidence {conf:.2f} >= high {self.confidence_high} "
                        f"→ T1 ({self.t1_model})"
                    ),
                )
            # T1 disabled (no local Ollama) — degrade to T2.
            return CascadeDecision(
                chosen_model=self.t2_model,
                tier=TIER_T2,
                reason=(
                    f"confidence {conf:.2f} >= high {self.confidence_high} "
                    f"but T1 disabled → T2 ({self.t2_model})"
                ),
            )

        if conf >= self.confidence_low:
            return CascadeDecision(
                chosen_model=self.t2_model,
                tier=TIER_T2,
                reason=(
                    f"confidence {conf:.2f} in [{self.confidence_low}, "
                    f"{self.confidence_high}) → T2 ({self.t2_model})"
                ),
            )

        return CascadeDecision(
            chosen_model=self.t3_model,
            tier=TIER_T3,
            reason=(
                f"confidence {conf:.2f} < low {self.confidence_low} "
                f"→ T3 ({self.t3_model})"
            ),
        )


# === Convenience: one-call helper ===

def select_tier(
    confidence: float,
    *,
    fallback: bool = False,
    t1_model: str | None = None,
    t2_model: str | None = None,
    t3_model: str | None = None,
    confidence_high: float | None = None,
    confidence_low: float | None = None,
) -> CascadeDecision:
    """Functional form of :meth:`TierSelector.select_tier`.

    Constructs a fresh :class:`TierSelector` per call. Convenient
    for one-shot decisions in CLI handlers and the merge queue.
    For repeated calls, instantiate a :class:`TierSelector` once
    and reuse it (it has no state, but the construction is cheap
    to avoid).
    """
    return TierSelector(
        t1_model=t1_model,
        t2_model=t2_model,
        t3_model=t3_model,
        confidence_high=confidence_high,
        confidence_low=confidence_low,
    ).select_tier(confidence, fallback=fallback)


__all__ = [
    "TIER_T1",
    "TIER_T2",
    "TIER_T3",
    "TIER_PRIORITY",
    "T1_DISABLED",
    "CascadeDecision",
    "TierSelector",
    "select_tier",
]
