"""Phase 4.0: Hook context and decision dataclasses.

All hook events share a single ``HookContext`` shape; event-specific
fields are placed in ``payload`` (dict). This keeps the registry
simple (event → [hooks], not event × field schemas).

Trust boundary: stdlib + dataclasses only. No production imports.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Literal


Decision = Literal["allow", "block", "modify"]
"""``allow`` = proceed, ``block`` = abort (exit 2 equivalent), ``modify`` = proceed with payload override."""


@dataclass(frozen=True)
class HookContext:
    """Payload for a single hook invocation.

    Attributes:
        event: ``EventType.value`` (canonical CC wire name).
        session_id: Current session UUID, or "" if not in a session.
        agent_id: Current agent id ("" for main session).
        payload: Event-specific dict. Schema is documented per event
            in ``docs/hooks.md``.
        ts: Unix epoch when the event was emitted.
        request_id: Optional: matches LLM call id (for cross-tracing).
        recursion_depth: Number of times a hook has fired inside
            another hook for the SAME event. Bounded by
            ``settings.hooks_max_recursion_depth`` (default 3).
        event_stack: Stack of enclosing events (for reentrancy guard).
    """

    event: str
    session_id: str
    agent_id: str
    payload: dict[str, Any]
    ts: float = field(default_factory=time.time)
    request_id: str = ""
    recursion_depth: int = 0
    event_stack: tuple[str, ...] = field(default_factory=tuple)

    def with_payload(self, new_payload: dict[str, Any]) -> "HookContext":
        """Return a copy with ``payload`` replaced (for modify decisions)."""
        return replace(self, payload=new_payload)

    def with_event(self, event: str) -> "HookContext":
        """Return a copy with ``event`` and ``event_stack`` advanced."""
        return replace(
            self,
            event=event,
            event_stack=self.event_stack + (self.event,),
            recursion_depth=self.recursion_depth + 1,
        )


@dataclass(frozen=True)
class HookDecision:
    """Result of a single hook execution.

    Attributes:
        decision: ``"allow"`` | ``"block"`` | ``"modify"``.
        output: For ``modify``: the new payload. For ``block``: a
            human-readable reason. For ``allow``: empty.
        error: Optional error message (e.g. timeout, exception).
        duration_ms: How long the hook took to execute.
        hook_id: ID of the hook that produced this decision.
    """

    decision: Decision
    hook_id: str
    duration_ms: float = 0.0
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation."""
        return {
            "decision": self.decision,
            "hook_id": self.hook_id,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookDecision":
        """Parse from JSON dict (subprocess / HTTP wire format)."""
        return cls(
            decision=data["decision"],
            hook_id=data.get("hook_id", "unknown"),
            duration_ms=float(data.get("duration_ms", 0.0)),
            output=dict(data.get("output", {})),
            error=str(data.get("error", "")),
        )


@dataclass(frozen=True)
class HookAggregate:
    """Combined result of all hooks for a single event.

    ``final_decision`` is computed by ``HookRunner``:
        - any ``block`` → ``block`` (first blocker's reason wins)
        - any ``modify`` → ``modify`` with the LAST modified payload
        - else → ``allow``

    Attributes:
        final_decision: Combined decision across all hooks.
        decisions: Per-hook decisions in dispatch order.
        blocked_by: ``hook_id`` of the first blocker (or "" if allowed).
        final_payload: The (possibly modified) payload to pass to
            downstream code.
    """

    final_decision: Decision
    decisions: tuple[HookDecision, ...]
    final_payload: dict[str, Any] = field(default_factory=dict)
    blocked_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation (for audit + WS)."""
        return {
            "final_decision": self.final_decision,
            "blocked_by": self.blocked_by,
            "final_payload": self.final_payload,
            "decisions": [d.to_dict() for d in self.decisions],
        }


def new_request_id() -> str:
    """Generate a short unique request id (for cross-hook tracing)."""
    return uuid.uuid4().hex[:12]


__all__ = [
    "Decision",
    "HookContext",
    "HookDecision",
    "HookAggregate",
    "new_request_id",
]
