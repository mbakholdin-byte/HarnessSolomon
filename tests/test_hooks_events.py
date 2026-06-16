"""Phase 4.0 + 4.3: Tests for EventType enum + ENABLED_BY_DEFAULT."""
from __future__ import annotations

import pytest

from harness.hooks import ENABLED_BY_DEFAULT, EventType
from harness.hooks.events import DEFERRED_EVENTS


class TestEventType:
    """EventType is a string enum; values are CC wire names."""

    def test_all_16_events_exist(self) -> None:
        """16 events = 13 CC (incl. Elicitation + Notification from Phase 4.3) + 3 custom."""
        assert len(EventType) == 16

    def test_cc_event_values_match_claude_code(self) -> None:
        """Values must be PascalCase strings matching CC docs."""
        expected = {
            "PreToolUse",
            "PostToolUse",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreCompact",
            "InstructionsLoaded",
            "PermissionRequest",
            # Phase 4.3: Elicitation + Notification are now implemented.
            "Elicitation",
            "Notification",
        }
        actual = {e.value for e in EventType}
        assert expected.issubset(actual)

    def test_custom_solomon_events_exist(self) -> None:
        """3 custom events: OnMemoryWrite, OnRoutingDecision, OnCompaction."""
        assert EventType.ON_MEMORY_WRITE.value == "OnMemoryWrite"
        assert EventType.ON_ROUTING_DECISION.value == "OnRoutingDecision"
        assert EventType.ON_COMPACTION.value == "OnCompaction"

    def test_phase43_events_exist(self) -> None:
        """Phase 4.3: Elicitation + Notification events are now real members."""
        assert EventType.ELICITATION.value == "Elicitation"
        assert EventType.NOTIFICATION.value == "Notification"

    def test_event_type_is_str_subclass(self) -> None:
        """String enum: can compare to str directly."""
        assert EventType.PRE_TOOL_USE == "PreToolUse"
        assert EventType.PRE_TOOL_USE.value == "PreToolUse"

    def test_deferred_events_set_is_empty_phase43(self) -> None:
        """In Phase 4.3 all 16 events are implemented."""
        assert DEFERRED_EVENTS == frozenset()

    def test_enabled_by_default_contains_all_implemented_events(self) -> None:
        """All 16 events enabled by default."""
        assert ENABLED_BY_DEFAULT == set(EventType)

    @pytest.mark.parametrize(
        "event",
        [
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
            EventType.ELICITATION,
            EventType.NOTIFICATION,
            EventType.ON_MEMORY_WRITE,
            EventType.ON_ROUTING_DECISION,
            EventType.ON_COMPACTION,
        ],
    )
    def test_all_events_iterate(self, event: EventType) -> None:
        """Each event is a real EventType member."""
        assert isinstance(event, EventType)
        assert event.value


class TestEventTypeFromString:
    """EventType can be constructed from its string value."""

    def test_from_value(self) -> None:
        assert EventType("PreToolUse") is EventType.PRE_TOOL_USE

    def test_from_value_phase43(self) -> None:
        assert EventType("Elicitation") is EventType.ELICITATION
        assert EventType("Notification") is EventType.NOTIFICATION

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            EventType("NotARealEvent")
