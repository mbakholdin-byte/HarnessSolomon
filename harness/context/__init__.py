"""Solomon Harness — context management (Phase 3).

Public API:
    - ``ContextCompactor`` — sliding window + LLM summary for long
      chat histories. Wired into ``AgentLoop`` and ``ChatSession``
      via dependency injection (default ``None`` → no-op).
"""
from __future__ import annotations

from harness.context.compaction import (
    ContextCompactor,
)

__all__ = [
    "ContextCompactor",
]
