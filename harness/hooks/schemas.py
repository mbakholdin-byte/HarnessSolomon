"""Phase 4.6 v1.16.0: Per-event Pydantic payload schemas.

One ``BaseModel`` per ``EventType``. The ``EVENT_SCHEMAS`` dict maps
the canonical CC wire name (``EventType.value``) to its model.

Design:
    - Schemas are **advisory** (fail-open): a validation failure in
      ``validate_payload`` logs a warning and returns the original
      payload. Hook dispatch must NEVER break because of a schema
      regression.
    - Schemas use ``model_config = ConfigDict(extra="ignore")`` so
      forward-compatible payloads with extra fields don't fail.
    - PII safety: ``OnMemoryWritePayload`` deliberately has NO
      ``value`` field — only ``key_hash`` (truncated SHA-256). This
      matches the emit site in ``harness/memory/unified.py``.

Trust boundary: stdlib + pydantic only. NO ``harness.agents`` or
``harness.server`` imports. Enforced by ``tests/test_hook_schemas.py``
(AST scan).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Forward-compat version. Bump when a breaking schema change lands.
# Consumers can inspect this to decide whether to coerce old shapes.
__version__ = "1"


# === CC event payloads ===


class PreToolUsePayload(BaseModel):
    """Fired before every tool call in ``ToolRuntime.execute``.

    Required fields: ``tool_name``, ``arguments``.
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str
    arguments: dict[str, Any]


class PostToolUsePayload(BaseModel):
    """Fired after every tool call.

    Required: ``tool_name``, ``arguments``. Optional: ``ok``,
    ``output``, ``error``.
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str
    arguments: dict[str, Any]
    ok: Optional[bool] = None
    output: Optional[str] = None
    error: Optional[str] = None


class StopPayload(BaseModel):
    """Fired when the agent loop exits.

    Required: ``reason``, ``final_message``, ``iterations``.
    """

    model_config = ConfigDict(extra="ignore")

    reason: str
    final_message: str
    iterations: int = Field(ge=0)


class SubagentStartPayload(BaseModel):
    """Fired before ``AgentRunner.run``.

    Required: ``agent_name``, ``prompt``, ``model``.
    """

    model_config = ConfigDict(extra="ignore")

    agent_name: str
    prompt: str
    model: str


class SubagentStopPayload(BaseModel):
    """Fired after ``AgentRunner.run``.

    Required: ``agent_name``, ``result``, ``duration_ms``.
    """

    model_config = ConfigDict(extra="ignore")

    agent_name: str
    result: str
    duration_ms: float = Field(ge=0.0)


class PreCompactPayload(BaseModel):
    """Fired before ``ContextCompactor.maybe_compact``.

    Required: ``messages_count``, ``tokens_estimate``.
    """

    model_config = ConfigDict(extra="ignore")

    messages_count: int = Field(ge=0)
    tokens_estimate: int = Field(ge=0)


class OnCompactionPayload(BaseModel):
    """Fired after ``ContextCompactor`` produces a summary.

    Required: ``session_id``, ``summary_preview``, ``saved_tokens``.

    Phase 4.13A v1.23.0 adds optional diagnostic fields
    (``agent_id``, ``pre_tokens``, ``post_tokens``, ``ratio``,
    ``trigger_reason``, ``cache_hit``) so the ``CompactTrigger``
    emission site can surface the full compaction shape without
    breaking the original ``ContextCompactor`` consumers.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    summary_preview: str
    saved_tokens: int = Field(ge=0)
    # Phase 4.13A v1.23.0 optional fields.
    agent_id: Optional[str] = None
    pre_tokens: Optional[int] = Field(default=None, ge=0)
    post_tokens: Optional[int] = Field(default=None, ge=0)
    ratio: Optional[float] = Field(default=None, ge=0.0)
    trigger_reason: Optional[str] = None
    cache_hit: Optional[bool] = None


class OnRoutingDecisionPayload(BaseModel):
    """Fired after ``LLMRouterClassifier.classify``.

    Required: ``chosen_agent``, ``confidence``, ``model``, ``trigger``.
    Optional: ``fallback``, ``task_preview``.

    Phase 4.13A v1.23.0 adds optional tier-cascade fields
    (``session_id``, ``agent_id``, ``prompt_tokens``,
    ``selected_tier``, ``model_id``, ``latency_ms``, ``cost_usd``)
    so the ``TierSelector.select`` emission site can surface the
    tier decision without breaking the original router consumers.
    """

    model_config = ConfigDict(extra="ignore")

    chosen_agent: str
    confidence: float = Field(ge=0.0, le=1.0)
    fallback: Optional[bool] = None
    model: str
    trigger: str
    task_preview: Optional[str] = None
    # Phase 4.13A v1.23.0 optional fields.
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    prompt_tokens: Optional[int] = Field(default=None, ge=0)
    selected_tier: Optional[str] = None
    model_id: Optional[str] = None
    latency_ms: Optional[float] = Field(default=None, ge=0.0)
    cost_usd: Optional[float] = Field(default=None, ge=0.0)


class UserPromptSubmitPayload(BaseModel):
    """Fired on every WebSocket user message.

    Required: ``prompt_preview``, ``session_id``.
    """

    model_config = ConfigDict(extra="ignore")

    prompt_preview: str
    session_id: str


class InstructionsLoadedPayload(BaseModel):
    """Fired when an ``AgentSpec`` is loaded from disk.

    Required: ``spec_name``, ``file_path``.
    """

    model_config = ConfigDict(extra="ignore")

    spec_name: str
    file_path: str


class OnMemoryWritePayload(BaseModel):
    """Fired inside ``UnifiedMemory.write`` (post-redaction, pre-persist).

    PII safety: this schema deliberately has NO ``value`` field. The
    emit site passes ``key_hash`` (truncated SHA-256 of the memory id),
    never the raw key or value.

    Required: ``layer``, ``key_hash``, ``scope``, ``size_bytes``.

    Phase 4.13A v1.23.0 adds optional diagnostic fields
    (``session_id``, ``agent_id``, ``value_size``, ``note_id``,
    ``timestamp``) for the L2 store emission site. ``value_size`` is
    a per-event synonym for ``size_bytes`` (the L2 vector store
    measures bytes, not memory record size); both fields carry
    identical numeric values when emitted from the L2 site. ``note_id``
    is the numeric scratchpad note id (not a raw secret key) and
    surfaces only when the write originates from the L2 store.
    """

    model_config = ConfigDict(extra="ignore")

    layer: str
    key_hash: str
    scope: str
    size_bytes: int = Field(ge=0)
    # Phase 4.13A v1.23.0 optional fields.
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    value_size: Optional[int] = Field(default=None, ge=0)
    note_id: Optional[int] = Field(default=None, ge=0)
    timestamp: Optional[float] = Field(default=None, ge=0.0)


class PermissionRequestPayload(BaseModel):
    """Fired when a tool would be denied by the denylist.

    Required: ``tool_name``, ``arguments_preview``,
    ``permission_decision``, ``denied_reason``.
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: str
    arguments_preview: str
    permission_decision: Literal["allow", "deny"]
    denied_reason: str = ""


class SessionStartPayload(BaseModel):
    """Fired on FastAPI lifespan startup.

    Required: ``session_id``, ``working_dir``.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    working_dir: str


class SessionEndPayload(BaseModel):
    """Fired on FastAPI lifespan shutdown.

    Required: ``session_id``, ``duration_seconds``.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    duration_seconds: float = Field(ge=0.0)


class ElicitationPayload(BaseModel):
    """Interactive prompt for the user/operator.

    Required: ``question``, ``options``, ``multi_select``,
    ``default_answer``. Optional: ``requires_confirmation`` (gate flag
    consumed by ``confirm_dangerous_hook``), ``answer`` /
    ``answer_source`` (output fields set by the hook after resolution).
    """

    model_config = ConfigDict(extra="ignore")

    question: str
    options: list[str] = Field(default_factory=list)
    multi_select: bool = False
    default_answer: Optional[str] = None
    # Phase 4.13C fix: previously these fields were dropped by the
    # extra="ignore" normalisation in validate_payload (Phase 4.6),
    # which caused confirm_dangerous_hook to short-circuit on
    # ``requires_confirmation`` being absent. Restoring them keeps the
    # schema in sync with the payload contract documented in
    # confirm_dangerous.py (v1.12.0).
    requires_confirmation: bool = False
    answer: Optional[str] = None
    answer_source: Optional[str] = None


class NotificationPayload(BaseModel):
    """Fire-and-forget push message.

    Required: ``severity``, ``message``. Optional: ``channels``.
    """

    model_config = ConfigDict(extra="ignore")

    severity: Literal["info", "warning", "error"]
    message: str
    channels: Optional[list[str]] = None


# === Registry ===

#: Maps the canonical CC wire name (``EventType.value``) to its
#: Pydantic model. Events not in this dict skip validation (backward
#: compat for unknown / future events).
EVENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "PreToolUse": PreToolUsePayload,
    "PostToolUse": PostToolUsePayload,
    "Stop": StopPayload,
    "SubagentStart": SubagentStartPayload,
    "SubagentStop": SubagentStopPayload,
    "PreCompact": PreCompactPayload,
    "OnCompaction": OnCompactionPayload,
    "OnRoutingDecision": OnRoutingDecisionPayload,
    "UserPromptSubmit": UserPromptSubmitPayload,
    "InstructionsLoaded": InstructionsLoadedPayload,
    "OnMemoryWrite": OnMemoryWritePayload,
    "PermissionRequest": PermissionRequestPayload,
    "SessionStart": SessionStartPayload,
    "SessionEnd": SessionEndPayload,
    "Elicitation": ElicitationPayload,
    "Notification": NotificationPayload,
}


__all__ = [
    "__version__",
    "EVENT_SCHEMAS",
    "PreToolUsePayload",
    "PostToolUsePayload",
    "StopPayload",
    "SubagentStartPayload",
    "SubagentStopPayload",
    "PreCompactPayload",
    "OnCompactionPayload",
    "OnRoutingDecisionPayload",
    "UserPromptSubmitPayload",
    "InstructionsLoadedPayload",
    "OnMemoryWritePayload",
    "PermissionRequestPayload",
    "SessionStartPayload",
    "SessionEndPayload",
    "ElicitationPayload",
    "NotificationPayload",
]
