"""LLM-as-router — classify a task into a sub-agent (Phase 2.0, Step 6).

The router takes a natural-language task and decides which sub-agent
should handle it. The classifier prompt lists the available
``AgentSpec``s (name, one-line role, tools, permissions) and asks the
model to respond with a JSON object::

    {"agent": "<name>", "confidence": 0.0-1.0}

We parse the response with a permissive regex; if the JSON is malformed
or the named agent is unknown, we set ``fallback=True`` and return the
first candidate (priority order: explore → plan → code → review).

**Cost-aware routing is a stub** in Phase 2.0: we always pick the LLM's
choice. The T1→T2→T3 cascade (using a cheap local model first, promoting
to cloud on low confidence) lands in Phase 2.1.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, Field

from harness.agents.spec import AgentSpec
from harness.config import settings
# Phase 4.4+ v1.14.0: OnRoutingDecision hook fires after classify().
from harness.hooks.runner import safe_fire
from harness.server.llm.router import CompletionResult, LLMRouter

logger = logging.getLogger(__name__)


# === Constants ===

#: System prompt for the classifier LLM. We use a minimal framing — the
#: candidate list is appended at call time.
ROUTER_SYSTEM_PROMPT: str = (
    "You are the Solomon sub-agent router. Given a user task and a list of "
    "available sub-agents, pick the best one.\n\n"
    "Respond with a single JSON object on one line: "
    '{"agent": "<name>", "confidence": 0.0-1.0}.\n'
    "No prose, no markdown, no extra keys."
)

#: Regex that pulls the JSON line out of a possibly-noisy model reply.
#: Captures the first {...} object that contains the key ``agent``.
_JSON_LINE_RE = re.compile(
    r'\{[^{}]*"agent"\s*:[^{}]*"confidence"\s*:[^{}]*\}',
    re.IGNORECASE,
)

#: Soft fallback — also accept the bare "agent: <name>" form for chatty models.
_BARE_AGENT_RE = re.compile(r'\bagent\s*:\s*"?([A-Za-z][A-Za-z0-9_-]*)"?', re.IGNORECASE)

#: Default order in which we pick a fallback agent when the model reply
#: is unusable. ``explore`` is safest (read-only, can't break anything);
#: ``code`` is the most general.
_FALLBACK_ORDER: tuple[str, ...] = ("explore", "plan", "code", "review")


# === Schema ===

class RouterDecision(BaseModel):
    """The router's choice of agent for a given task."""

    agent: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    fallback: bool = False
    raw_response: str = ""  # the model's full reply (for debugging)
    #: Optional tier name (``T1``/``T2``/``T3``) attached by the
    #: cascade after the fact (Phase 2.1). The router itself does
    #: NOT set this — it's filled in by
    #: :class:`~harness.agents.cascade.TierSelector` and is purely
    #: for observability / logging. Kept optional to preserve
    #: Phase 2.0 callers that construct ``RouterDecision`` directly.
    tier: str | None = None


# === Classifier ===

class LLMRouterClassifier:
    """Classify a task into one of the available :class:`AgentSpec`s.

    Args:
        router:        An :class:`LLMRouter` (reuse the main harness router).
        project_root:  Directory whose ``.harness/agents/`` contains overrides
                       and whose ``harness/agents/builtin/`` ships the
                       built-ins. Used to enumerate the candidate set when
                       ``candidates`` is not supplied.
    """

    def __init__(self, router: LLMRouter, *, project_root: Path) -> None:
        self.router = router
        self.project_root = Path(project_root).resolve(strict=False)
        # Imported lazily to avoid an import cycle (registry imports spec, but
        # we also live in harness.agents.*).
        from harness.agents.registry import all_specs

        self._all_specs_fn = all_specs

    async def classify(
        self,
        task: str,
        *,
        candidates: Sequence[AgentSpec] | None = None,
        model: str | None = None,
    ) -> RouterDecision:
        """Pick an agent for ``task``.

        Args:
            task:       The user's task description (any length; we truncate
                        defensively at 8000 chars to fit model context).
            candidates: Optional explicit list. When ``None``, we use
                        :func:`harness.agents.registry.all_specs` over
                        ``self.project_root``.
            model:      Model id (default: ``settings.subagent_default_model``).
        """
        specs: list[AgentSpec] = list(
            candidates if candidates is not None
            else self._all_specs_fn(project_root=self.project_root).values()
        )
        if not specs:
            raise ValueError("no candidate sub-agents available for routing")

        # Truncate long tasks to keep the classifier prompt bounded.
        MAX_TASK_LEN = 8000
        task_truncated = task if len(task) <= MAX_TASK_LEN else task[:MAX_TASK_LEN] + "\n…[truncated]"

        messages: list[dict] = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT + "\n\n" + self._format_candidates(specs)},
            {"role": "user", "content": task_truncated},
        ]
        used_model = model or settings.subagent_default_model
        try:
            response: CompletionResult = await self.router.completion(
                messages=messages, model=used_model, temperature=0.0,
            )
        except Exception as e:
            logger.warning("router LLM call failed: %s; falling back", e)
            decision = RouterDecision(
                agent=_first_available(specs), fallback=True,
                confidence=0.0, raw_response=str(e),
            )
            await self._fire_routing_hook(decision, used_model, task, "llm_error")
            return decision

        result = await self._parse(response, specs, task=task, used_model=used_model)
        trigger = "fallback_exhausted" if result.fallback else "user_prompt"
        if result.confidence < 0.5 and not result.fallback:
            trigger = "low_confidence"
        await self._fire_routing_hook(
            result, used_model or settings.subagent_default_model, task, trigger,
        )
        return result

    # --- helpers ---

    @staticmethod
    def _format_candidates(specs: Sequence[AgentSpec]) -> str:
        lines: list[str] = ["Available sub-agents:"]
        for s in specs:
            one_liner = s.system_prompt.splitlines()[0] if s.system_prompt else "(no role)"
            one_liner = one_liner[:80]
            lines.append(
                f"- name={s.name!r} perms={s.permissions} tools={s.tools} — {one_liner}"
            )
        return "\n".join(lines)

    @staticmethod
    async def _parse(
        response: CompletionResult,
        specs: Sequence[AgentSpec],
        *,
        task: str = "",
        used_model: str = "",
    ) -> RouterDecision:
        """Extract ``(agent, confidence)`` from the LLM response.

        Tries the strict JSON form first, then a bare ``agent: <name>``
        line, and finally falls back to the first candidate in
        :data:`_FALLBACK_ORDER`.

        ``task`` and ``used_model`` are passed through for hook metadata
        (fired by the caller — ``classify()``).
        """
        content = (response.content or "").strip()
        raw = content

        # 1. Strict JSON.
        m = _JSON_LINE_RE.search(content)
        if m:
            try:
                data = json.loads(m.group(0))
                name = str(data.get("agent", "")).strip()
                conf = float(data.get("confidence", 1.0))
                if name in {s.name for s in specs}:
                    decision = RouterDecision(
                        agent=name, confidence=conf, fallback=False, raw_response=raw,
                    )
                    # NOTE: trigger = "user_prompt"; hook fired by classify()
                    return decision
            except (json.JSONDecodeError, ValueError, TypeError):
                pass  # fall through

        # 2. Bare ``agent: <name>`` form.
        m = _BARE_AGENT_RE.search(content)
        if m:
            name = m.group(1)
            if name in {s.name for s in specs}:
                return RouterDecision(
                    agent=name, confidence=0.5, fallback=False, raw_response=raw,
                )

        # 3. Fallback: first candidate in priority order that's available.
        for preferred in _FALLBACK_ORDER:
            for s in specs:
                if s.name == preferred:
                    return RouterDecision(
                        agent=preferred, confidence=0.0, fallback=True, raw_response=raw,
                    )
        # Should not happen — we already checked candidates is non-empty.
        return RouterDecision(
            agent=specs[0].name, confidence=0.0, fallback=True, raw_response=raw,
        )

    async def _fire_routing_hook(
        self,
        decision: "RouterDecision",
        model: str,
        task: str,
        trigger: str,
    ) -> None:
        """Phase 4.4+ v1.14.0: fire OnRoutingDecision hook.

        ``trigger`` is one of:
          - "user_prompt" — top-level user task classification
          - "l2_curator" — L2 scratchpad search
          - "l2_promote" — L2 promotion to L1
          - "fallback_exhausted" — all fallbacks exhausted
          - "low_confidence" — LLM confidence below threshold

        Block semantics: an OnRoutingDecision block is currently
        logged-only (we return the same decision; full re-route
        behavior is a Phase 4.5 concern).
        """
        try:
            await safe_fire(
                "OnRoutingDecision",
                session_id="",
                agent_id=decision.agent,
                payload={
                    "chosen_agent": decision.agent,
                    "confidence": decision.confidence,
                    "fallback": decision.fallback,
                    "model": model,
                    "trigger": trigger,
                    "task_preview": task[:200] if task else "",
                },
            )
        except Exception:  # noqa: BLE001 — hooks must never break routing
            pass


def _first_available(specs: Sequence[AgentSpec]) -> str:
    """Return the first agent name from the fallback order that's in specs."""
    for preferred in _FALLBACK_ORDER:
        for s in specs:
            if s.name == preferred:
                return preferred
    return specs[0].name
