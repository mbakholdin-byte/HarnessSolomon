"""Phase 4.0: LLM-as-hook transport.

An LLM hook uses a small language model to decide whether to allow,
block, or modify a context. Wire format:
    1. Runner sends prompt to LLM.
    2. LLM responds with JSON ``{"decision": ..., "reason": "..."}``.
    3. Decision is extracted and returned.

The LLM router is injected via DI to maintain trust boundary
(B1): ``harness.hooks.llm_hook`` does NOT import
``harness.server.llm.router`` at module level. The router type is
declared as a structural ``Protocol`` so production code can pass
any object with a ``.completion(messages, model)`` method.

Defence in depth (Plan § 8.3):
    - LLM hook output is bounded (200 chars max for reason).
    - Decision is one of 3 literals.
    - Modify payload is bounded to 1KB.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger(__name__)


class _RouterProto(Protocol):
    """Minimal protocol — anything with ``.completion(messages, model)`` works."""

    async def completion(
        self, *, messages: list[dict[str, str]], model: str
    ) -> Any:  # returns an object with .content or str
        ...


# Same permissive JSON-extraction regex as LLMRouterClassifier.
_JSON_LINE_RE = re.compile(r"\{[^{}]*\"decision\"[^{}]*\}", re.DOTALL)


def _extract_json_decision(text: str) -> dict[str, Any] | None:
    """Extract a JSON object with a ``decision`` field from LLM output.

    Permissive: tries full parse first, then regex fallback (mirrors
    ``LLMRouterClassifier._extract_decision``). Returns ``None`` if
    no valid JSON found.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = _JSON_LINE_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None


class LLMHook:
    """Single LLM-as-hook instance.

    Construction:
        ``LLMHook(router=app.state.llm_router)`` — router injected
        via DI to keep trust boundary clean (no module-level import
        of ``harness.server.llm.router``).

    Usage (in a hook callable):
        ``hook = LLMHook(router=..., model="qwen3-8b", prompt="...")``
        ``await hook(context)``
    """

    def __init__(
        self,
        router: _RouterProto,
        *,
        model: str,
        prompt: str,
        timeout_ms: int = 3000,
    ) -> None:
        self._router = router
        self._model = model
        self._prompt = prompt
        self._timeout_ms = timeout_ms

    async def __call__(self, context: HookContext) -> HookDecision:
        start = time.monotonic()
        # Build the prompt.
        prompt_text = self._prompt.format(
            event=context.event,
            payload=json.dumps(context.payload, ensure_ascii=False),
            session_id=context.session_id,
            agent_id=context.agent_id,
        )
        messages = [
            {
                "role": "system",
                "content": "You are a hook deciding whether to allow, block, or "
                "modify a tool/event invocation. Respond with JSON: "
                '{"decision": "allow"|"block"|"modify", "reason": "..."}',
            },
            {"role": "user", "content": prompt_text},
        ]
        try:
            import asyncio

            response = await asyncio.wait_for(
                self._router.completion(messages=messages, model=self._model),
                timeout=self._timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000.0
            logger.warning(
                "LLM hook (model=%s) timed out after %dms",
                self._model,
                self._timeout_ms,
            )
            return HookDecision(
                decision="allow",  # fail-open
                hook_id=f"llm.{self._model}",
                duration_ms=duration_ms,
                error=f"LLM timeout after {self._timeout_ms}ms",
            )
        except Exception as e:  # noqa: BLE001
            duration_ms = (time.monotonic() - start) * 1000.0
            logger.warning(
                "LLM hook (model=%s) raised %s: %s",
                self._model,
                type(e).__name__,
                e,
            )
            return HookDecision(
                decision="allow",
                hook_id=f"llm.{self._model}",
                duration_ms=duration_ms,
                error=f"{type(e).__name__}: {e}",
            )

        duration_ms = (time.monotonic() - start) * 1000.0
        # Extract response text.
        text = ""
        if isinstance(response, str):
            text = response
        elif hasattr(response, "content"):
            text = str(response.content)
        elif hasattr(response, "text"):
            text = str(response.text)
        else:
            text = str(response)

        data = _extract_json_decision(text)
        if data is None:
            return HookDecision(
                decision="allow",
                hook_id=f"llm.{self._model}",
                duration_ms=duration_ms,
                error=f"could not parse JSON decision from: {text[:200]}",
            )

        decision_str = data.get("decision", "allow")
        if decision_str not in ("allow", "block", "modify"):
            decision_str = "allow"
        # Build output dict: include "reason" (capped at 200) and
        # "payload" (for modify) bounded to 1KB per Plan § 8.3.
        output: dict[str, Any] = {}
        if "reason" in data:
            output["reason"] = str(data["reason"])[:200]
        if "payload" in data and isinstance(data["payload"], dict):
            payload_str = json.dumps(data["payload"])
            if len(payload_str) <= 1024:
                output["payload"] = data["payload"]
            else:
                output["payload_truncated"] = True
        return HookDecision(
            decision=decision_str,  # type: ignore[arg-type]
            hook_id=f"llm.{self._model}",
            duration_ms=duration_ms,
            output=output,
        )


__all__ = ["LLMHook", "_extract_json_decision"]
