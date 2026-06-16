"""Phase 4.3+ v1.12.0: Builtin ConfirmDangerousHook — Elicitation for risk gates.

Default ON (when Elicitation event is enabled). Listens to
``Elicitation`` events whose payload carries a
``requires_confirmation=True`` flag.

Behaviour (Phase 4.3+ v1.12.0, evolved from v1.10.0):

    1. **If a WebSocket client is connected** to
       ``/api/v1/elicitation/ws`` (``hooks_elicitation_ws_enabled=True``),
       the hook publishes the question to ``ElicitationBroker`` and
       awaits a human answer (timeout = ``hooks_elicitation_ws_timeout_s``,
       default 30.0s). The user's real answer becomes ``payload["answer"]``.
    2. **If no WS client responds in time** (or WS is disabled), the
       hook falls back to the default answer (typically ``"abort"``).
    3. The agent loop is never blocked: every code path returns an
       answer within the timeout. Fail-open is the design principle.

Payload contract::

    {
        "question": "Run rm -rf /tmp/foo? [y/N]",
        "options": ["proceed", "abort"],
        "multi_select": False,
        "default_answer": "abort",
        "tool_name": "bash",                # optional, for matching
        "requires_confirmation": True,      # gate flag
    }

Decision shape: always ``modify`` (with the resolved answer in
``payload["answer"]`` and ``payload["answer_source"]`` set to one of
``"ws_human"`` / ``"default_timeout"`` / ``"default_ws_disabled"``).
"""
from __future__ import annotations

import logging

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.confirm_dangerous")


async def confirm_dangerous_hook(context: HookContext) -> HookDecision:
    """Inject an answer for Elicitation prompts that need confirmation.

    Phase 4.3+ v1.12.0: optionally routes through the ElicitationBroker
    for real-time WebSocket answers; falls back to the default answer
    if no human responds within the timeout.
    """
    if context.event != "Elicitation":
        return HookDecision(decision="allow", hook_id="builtin.confirm_dangerous")
    payload = context.payload
    if not payload.get("requires_confirmation"):
        # Not a confirmation-gated prompt — let other hooks decide.
        return HookDecision(decision="allow", hook_id="builtin.confirm_dangerous")
    default = payload.get("default_answer", "abort")
    question = payload.get("question", "")

    # Phase 4.3+ v1.12.0: try the WebSocket broker if enabled.
    answer, source = await _resolve_answer(
        question=question,
        options=payload.get("options") or [],
        default_answer=default,
    )

    new_payload = dict(payload)
    new_payload["answer"] = answer
    new_payload["answer_source"] = source
    logger.debug(
        "ConfirmDangerous: answer=%r source=%s for question=%r",
        answer, source, question[:80],
    )
    return HookDecision(
        decision="modify",
        hook_id="builtin.confirm_dangerous",
        output={"payload": new_payload},
    )


async def _resolve_answer(
    *,
    question: str,
    options: list[str],
    default_answer: str,
) -> tuple[str, str]:
    """Resolve the answer via WebSocket broker, fall back to default.

    Returns (answer, source) where source ∈ {ws_human, default_timeout,
    default_ws_disabled}.
    """
    # Lazy import to keep confirm_dangerous importable in tests without
    # the broker (and to avoid circular import).
    from harness.config import Settings
    from harness.elicitation import ElicitationBroker

    settings = Settings()
    if not settings.hooks_elicitation_ws_enabled:
        return (default_answer, "default_ws_disabled")
    broker = ElicitationBroker.get()
    qid = broker.publish(
        question=question,
        options=options,
        default_answer=default_answer,
        timeout_s=settings.hooks_elicitation_ws_timeout_s,
    )
    try:
        answer = await broker.wait(qid)
    except Exception as e:  # noqa: BLE001 — never break the agent loop
        logger.warning("ConfirmDangerous: broker.wait failed (%s): %s", type(e).__name__, e)
        return (default_answer, "default_timeout")
    # The broker returns the default after timeout, the user's value otherwise.
    if answer == default_answer:
        # Either the user really chose the default OR the timeout fired.
        # We can't distinguish at this layer (broker already dropped the
        # entry) — best-effort: check the broker's stats counter.
        stats = broker.stats()
        if stats.get("timed_out_total", 0) > 0:
            return (answer, "default_timeout")
    return (answer, "ws_human")
