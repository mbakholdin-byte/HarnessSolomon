"""Phase 4.0 + Phase 4.3: Hook event types.

Defines the 14 Claude Code hook events + 3 custom Solomon Harness
events. Elicitation and Notification were deferred from Phase 4.0
and shipped in Phase 4.3 (v1.10.0) — they are now part of the
``ENABLED_BY_DEFAULT`` set.

Trust boundary: this module is stdlib only. No ``harness.agents``
or ``harness.server`` imports.
"""
from __future__ import annotations

import enum


class EventType(str, enum.Enum):
    """All hook events supported by Phase 4.0.

    Value is the canonical CC wire name (PascalCase). Settings that
    take a list of event names should compare against ``.value``.

    Members:
        PRE_TOOL_USE / POST_TOOL_USE: fired around every tool call
            (``ToolRuntime.execute``). Payload includes tool_name +
            arguments + result.
        STOP: fired when the agent loop exits (max iterations or
            explicit stop). Payload includes reason + final message.
        SUBAGENT_START / SUBAGENT_STOP: fired around every
            ``AgentRunner.run`` invocation. Payload includes
            agent_name + prompt + model + result.
        SESSION_START / SESSION_END: fired on FastAPI lifespan
            start/end. Payload includes session_id (None for start)
            + working_dir.
        USER_PROMPT_SUBMIT: fired on every WebSocket user message
            before the agent loop. Payload includes prompt.
        PRE_COMPACT: fired before ``ContextCompactor.maybe_compact``
            runs (allows hooks to snapshot state).
        INSTRUCTIONS_LOADED: fired when an ``AgentSpec`` is loaded
            from disk. Payload includes spec_name + file_path.
        PERMISSION_REQUEST: fired when a tool would be denied by the
            existing denylist. Hook may override to allow.
        ON_MEMORY_WRITE: fired inside ``UnifiedMemory.write``
            (post-redaction, pre-persist).
        ON_ROUTING_DECISION: fired after ``LLMRouterClassifier.classify``
            resolves a tier. Read-only (decision=modify overrides).
        ON_COMPACTION: fired after ``ContextCompactor`` produces a
            summary (cache-miss only unless opt-in).
    """

    # === 12 CC events ===
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_COMPACT = "PreCompact"
    INSTRUCTIONS_LOADED = "InstructionsLoaded"
    PERMISSION_REQUEST = "PermissionRequest"
    # Phase 4.3: Elicitation + Notification are now implemented.
    # ELICITATION: interactive prompt for the user/operator. Hooks may
    #   ``modify`` the payload to inject a default answer or ``block`` to
    #   refuse the request. Schema: ``{question, options, multi_select,
    #   default_answer}``. Decision shape: ``allow`` (proceed with
    #   default), ``modify`` (override answer), ``block`` (refuse).
    # NOTIFICATION: fire-and-forget push message. Always ``allow`` —
    #   payload has no semantic effect on agent behavior. Schema:
    #   ``{severity, message, channels}`` (channels ∈ stdout/webhook/desktop).
    ELICITATION = "Elicitation"
    NOTIFICATION = "Notification"

    # === 3 custom Solomon events ===
    ON_MEMORY_WRITE = "OnMemoryWrite"
    ON_ROUTING_DECISION = "OnRoutingDecision"
    ON_COMPACTION = "OnCompaction"


# Phase 4.0 + 4.3: events that are NOT yet implemented (settings validator
# rejects enabling these).
DEFERRED_EVENTS: frozenset[EventType] = frozenset()
"""All 17 events are implemented (Phase 4.0 + Phase 4.3). Empty for now."""


# Phase 4.0 + 4.3: events that are implemented and enabled by default.
ENABLED_BY_DEFAULT: frozenset[EventType] = frozenset(
    {
        EventType.PRE_TOOL_USE,
        EventType.POST_TOOL_USE,
        EventType.STOP,
        EventType.SUBAGENT_START,
        EventType.SUBAGENT_STOP,
        EventType.SESSION_START,
        EventType.SESSION_END,
        EventType.USER_PROMPT_SUBMIT,
        EventType.PRE_COMPACT,
        EventType.INSTRUCTIONS_LOADED,
        EventType.PERMISSION_REQUEST,
        EventType.ON_MEMORY_WRITE,
        EventType.ON_ROUTING_DECISION,
        EventType.ON_COMPACTION,
        # Phase 4.3: Elicitation + Notification join the enabled set.
        EventType.ELICITATION,
        EventType.NOTIFICATION,
    }
)


__all__ = ["EventType", "DEFERRED_EVENTS", "ENABLED_BY_DEFAULT"]
