"""Phase 4.0: Public API for the hooks framework.

Importing from this top-level module is the only sanctioned way for
production code to interact with hooks. Production code MUST NOT
import from ``harness.hooks.builtin`` directly (that's for tests
and explicit registration only).

Trust boundary: this module does NOT import from ``harness.agents``
or ``harness.server``. The trust boundary is enforced by
``tests/test_hooks_trust_boundary.py``.

Public API surface:
    - ``EventType`` — the 15 supported events.
    - ``HookContext`` / ``HookDecision`` / ``HookAggregate`` — payload types.
    - ``HookSpec`` / ``HookRegistry`` — registration.
    - ``HookRunner`` — dispatch + timeout.
    - ``HttpHookTransport`` / ``LLMHook`` — external transports.
"""
from __future__ import annotations

from harness.hooks.context import (
    Decision,
    HookAggregate,
    HookContext,
    HookDecision,
    new_request_id,
)
from harness.hooks.elicitation import (
    ELICITATION_VALID_ANSWERS,
    NOTIFICATION_VALID_CHANNELS,
    NOTIFICATION_VALID_SEVERITIES,
    is_valid_elicitation_payload,
    is_valid_notification_payload,
)
from harness.hooks.events import ENABLED_BY_DEFAULT, EventType
from harness.hooks.registry import HookRegistry, HookSpec, HookTransport
from harness.hooks.runner import HookRunner

__all__ = [
    # Events
    "EventType",
    "ENABLED_BY_DEFAULT",
    # Payload types
    "Decision",
    "HookContext",
    "HookDecision",
    "HookAggregate",
    "new_request_id",
    # Registration
    "HookSpec",
    "HookRegistry",
    "HookTransport",
    # Runner
    "HookRunner",
    # Phase 4.3: Elicitation + Notification schema helpers
    "ELICITATION_VALID_ANSWERS",
    "NOTIFICATION_VALID_SEVERITIES",
    "NOTIFICATION_VALID_CHANNELS",
    "is_valid_elicitation_payload",
    "is_valid_notification_payload",
]
