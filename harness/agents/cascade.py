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

import asyncio
import logging
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from harness.config import settings
from harness.hooks.runner import safe_fire  # Phase 4.13A v1.23.0: OnRoutingDecision

logger = logging.getLogger(__name__)


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

    # === Phase 6.1A v1.26.0: Heuristic tier selector ================
    # Lightweight heuristic routing that runs BEFORE the confidence-
    # based cascade. Returns T1 / T2 / T3 or None (fall through to
    # the confidence cascade). The heuristic uses only prompt length,
    # context size, tool-call presence, and keyword matching — no LLM
    # calls, no I/O. Cost: O(len(prompt)) for keyword scan.

    def select_heuristic(
        self,
        prompt: str,
        context_size: int = 0,
        *,
        has_tool_calls: bool = False,
    ) -> str | None:
        """Heuristic tier routing — runs before the confidence cascade.

        Rules (checked in order, first match wins):

          1. **T1** (cheap local) if:
             - ``len(prompt) < t1_max_prompt_chars`` AND
             - ``context_size < t1_max_context_tokens`` AND
             - NOT ``has_tool_calls``

          2. **T3** (premium) if:
             - ``len(prompt) > t3_min_prompt_chars`` OR
             - ``context_size > t3_min_context_tokens`` OR
             - prompt contains any complexity keyword

          3. **T2** (mid-tier) as the default for everything else.

        Returns ``None`` when ``tier_routing_heuristic_enabled`` is
        ``False`` — the caller then falls through to the explicit
        ``model:`` from config (current Phase 2.1 behaviour).

        Args:
            prompt:          The user prompt text.
            context_size:    Current context window usage in tokens
                             (0 when unknown — default). Callers should
                             pass cumulative context from
                             :func:`AgentContext.get_context_size()
                             <harness.agents.context.AgentContext.get_context_size>`
                             for better routing.
            has_tool_calls:  ``True`` if the prompt includes tool-call
                             results or the agent loop expects tool
                             calls in this turn. Disqualifies T1
                             (tool calls need a capable model).

        Returns:
            One of ``"T1"``, ``"T2"``, ``"T3"``, or ``None`` when
            the heuristic is disabled.
        """
        if not getattr(settings, "tier_routing_heuristic_enabled", True):
            return None

        prompt_len = len(prompt)

        # T1: short prompt, small context, no tools.
        if (
            prompt_len < settings.tier_routing_t1_max_prompt_chars
            and context_size < settings.tier_routing_t1_max_context_tokens
            and not has_tool_calls
        ):
            return TIER_T1

        # T3: long prompt, huge context, or complexity keywords.
        prompt_lower = prompt.lower()
        keywords = getattr(settings, "tier_routing_complexity_keywords", [])
        if (
            prompt_len > settings.tier_routing_t3_min_prompt_chars
            or context_size > settings.tier_routing_t3_min_context_tokens
            or any(kw in prompt_lower for kw in keywords)
        ):
            return TIER_T3

        # T2: everything else (medium prompts without complexity signals).
        return TIER_T2

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

    # === Phase 4.13A v1.23.0: select() with OnRoutingDecision hook ========

    def select(
        self,
        confidence: float,
        *,
        fallback: bool = False,
        prompt_tokens: int = 0,
        session_id: str = "",
        agent_id: str = "",
    ) -> CascadeDecision:
        """Phase 4.13A: ``select_tier`` + ``OnRoutingDecision`` fire.

        This is the **instrumented** entry point for tier selection.
        It delegates to :meth:`select_tier` for the actual decision
        logic (so all unit tests of the threshold cascade keep
        working unchanged), measures the selection latency, and
        fires the ``OnRoutingDecision`` hook with the Phase 4.13A
        payload:

            {session_id, agent_id, prompt_tokens, selected_tier,
             model_id, latency_ms, cost_usd}

        Cost is left at ``0.0`` here — the TierSelector has no
        access to the cost table. The hook consumer (or a follow-up
        ``emit_llm_call``) is responsible for computing the actual
        USD cost from ``model_id`` + ``prompt_tokens``.

        Hot-path: ``safe_fire`` is async; this method is sync. We
        schedule the fire via ``loop.create_task`` when a running
        event loop is available, and swallow the ``RuntimeError``
        otherwise (tests, CLI, REPL). The selection decision itself
        is returned synchronously and never blocked by the hook.

        Args:
            confidence: Router self-confidence in ``[0, 1]``.
            fallback:   ``True`` if the router fell back.
            prompt_tokens: Prompt size for the upcoming LLM call
                (informational; ``0`` when unknown).
            session_id:   Session id (propagated to the hook).
            agent_id:     Agent id (propagated to the hook).

        Returns:
            The :class:`CascadeDecision` from :meth:`select_tier`.
        """
        start = time.monotonic()
        decision = self.select_tier(confidence, fallback=fallback)
        latency_ms = round((time.monotonic() - start) * 1000.0, 3)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                safe_fire(
                    "OnRoutingDecision",
                    session_id=session_id,
                    agent_id=agent_id,
                    payload={
                        # Phase 4.13A spec fields.
                        "session_id": session_id,
                        "agent_id": agent_id,
                        "prompt_tokens": int(prompt_tokens),
                        "selected_tier": decision.tier,
                        "model_id": decision.chosen_model,
                        "latency_ms": latency_ms,
                        "cost_usd": 0.0,
                        # Diagnostic fields for hook consumers.
                        "confidence": float(confidence),
                        "fallback": bool(fallback),
                        "reason": decision.reason,
                    },
                )
            )
        except RuntimeError:
            # No running event loop — fire-and-forget is not possible.
            # We intentionally do NOT call ``asyncio.run`` here because
            # the sync contract of ``select`` must not block on a hook
            # that may have transports with non-trivial latency.
            pass
        except Exception:  # noqa: BLE001 — hot path must never break
            logger.debug(
                "OnRoutingDecision safe_fire failed for tier=%s",
                decision.tier,
                exc_info=True,
            )
        return decision


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
