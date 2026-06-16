"""Phase 4.3: Elicitation + Notification payload schema helpers.

Lightweight validation (no pydantic dependency) for the canonical
payload shapes of the two new hook events. The hooks framework
itself treats ``payload`` as a free-form dict (so transports can
extend), but builtin hooks and external clients benefit from
documented shapes + structural checks.

Trust boundary: stdlib only.
"""
from __future__ import annotations

from typing import Any

# === Elicitation ===

ELICITATION_VALID_ANSWERS: tuple[str, ...] = (
    "proceed",
    "abort",
    "yes",
    "no",
    "custom",
)
"""Reserved answer values. Hooks may emit any string for ``answer`` —
this list is only used by schema helpers to flag obviously malformed
payloads during development."""


def is_valid_elicitation_payload(payload: dict[str, Any]) -> bool:
    """Check that the payload matches the Elicitation schema.

    Required:
        - ``question`` (non-empty string)
    Optional:
        - ``options`` (list of strings)
        - ``multi_select`` (bool)
        - ``default_answer`` (string)
        - ``answer`` (string) — set by the hook on modify decision
        - ``requires_confirmation`` (bool)
        - ``answer_source`` (string) — set by the hook on modify
    """
    if not isinstance(payload, dict):
        return False
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return False
    options = payload.get("options")
    if options is not None and not (
        isinstance(options, list)
        and all(isinstance(o, str) for o in options)
    ):
        return False
    multi_select = payload.get("multi_select")
    if multi_select is not None and not isinstance(multi_select, bool):
        return False
    default_answer = payload.get("default_answer")
    if default_answer is not None and not isinstance(default_answer, str):
        return False
    answer = payload.get("answer")
    if answer is not None and not isinstance(answer, str):
        return False
    requires_confirmation = payload.get("requires_confirmation")
    if requires_confirmation is not None and not isinstance(
        requires_confirmation, bool
    ):
        return False
    return True


# === Notification ===

NOTIFICATION_VALID_SEVERITIES: tuple[str, ...] = ("info", "warn", "error")
NOTIFICATION_VALID_CHANNELS: tuple[str, ...] = ("stdout", "webhook", "desktop")


def is_valid_notification_payload(payload: dict[str, Any]) -> bool:
    """Check that the payload matches the Notification schema.

    Required:
        - ``message`` (non-empty string)
    Optional:
        - ``severity`` (one of ``info`` / ``warn`` / ``error``)
        - ``channels`` (list of strings from the canonical set)
    """
    if not isinstance(payload, dict):
        return False
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return False
    severity = payload.get("severity")
    if severity is not None and severity not in NOTIFICATION_VALID_SEVERITIES:
        return False
    channels = payload.get("channels")
    if channels is not None:
        if not isinstance(channels, list):
            return False
        for ch in channels:
            if not isinstance(ch, str) or ch not in NOTIFICATION_VALID_CHANNELS:
                return False
    return True


__all__ = [
    "ELICITATION_VALID_ANSWERS",
    "NOTIFICATION_VALID_SEVERITIES",
    "NOTIFICATION_VALID_CHANNELS",
    "is_valid_elicitation_payload",
    "is_valid_notification_payload",
]
