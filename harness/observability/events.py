"""Phase 4.1: LogEvent — structured log payload.

Single dataclass for all observability events. Trust boundary: stdlib +
dataclasses only. No production imports.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogStatus = Literal["ok", "error", "timeout", "cancelled"]


@dataclass(frozen=True)
class LogEvent:
    """Structured log payload.

    One LogEvent == one JSONL line. Frozen so it can be safely passed
    across threads (no mutation after emit).

    Attributes:
        event: Canonical event name, e.g. ``"llm_call"``, ``"tool_call"``,
            ``"hook_dispatch"``, ``"compaction"``, ``"request_started"``,
            ``"request_finished"``, ``"cascade_decision"``,
            ``"routing_decision"``, ``"merge_queue_event"``,
            ``"outbound_delivery"``, ``"privacy_zone"``,
            ``"webhook_inbound"``, ``"session_lifecycle"``,
            ``"memory_write"``, ``"cost_accumulated"``.
        payload: Event-specific data (model, tokens, tool_name, etc.).
            NO PII — caller MUST redact before emit (mirror hooks B11).
        level: Log severity. Default ``"INFO"``.
        session_id: Current session UUID, or "" if not in a session.
        agent_id: Current agent id ("" for main session).
        request_id: Short unique id, matches LLM call id.
        trace_id: 32-char hex (W3C); "" if no active span.
        span_id: 16-char hex; "" if no active span.
        latency_ms: Optional: time elapsed (for completion events).
        status: ``"ok"`` / ``"error"`` / ``"timeout"`` / ``"cancelled"``.
        error: Optional error message.
    """

    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    level: LogLevel = "INFO"
    session_id: str = ""
    agent_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    latency_ms: float | None = None
    status: LogStatus = "ok"
    error: str | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation for JSONL output."""
        return asdict(self)


__all__ = ["LogEvent", "LogLevel", "LogStatus"]
