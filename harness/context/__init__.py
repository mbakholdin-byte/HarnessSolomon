"""Solomon Harness — context management (Phase 3).

Public API:
    - ``ContextCompactor`` — sliding window + LLM summary for long
      chat histories. Wired into ``AgentLoop`` and ``ChatSession``
      via dependency injection (default ``None`` → no-op).
    - ``CompactResult`` (Phase 3 v1.4.0) — structured result of
      a manual ``/compact`` invocation (returned by
      ``ContextCompactor.force_compact``).
"""
from __future__ import annotations

from harness.context.compaction import (
    CompactResult,
    ContextCompactor,
)

__all__ = [
    "CompactResult",
    "ContextCompactor",
]
