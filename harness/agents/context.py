"""Phase 7.6: Per-session agent context tracking.

Tracks cumulative LLM usage (prompt_tokens, completion_tokens)
across turns so that the tier router receives real context_size.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentContext:
    """Per-session context state for tier routing."""

    session_id: str = ""
    cumulative_prompt_tokens: int = 0
    cumulative_completion_tokens: int = 0
    last_context_size: int = 0  # prompt_tokens from last LLM call
    turn_count: int = 0

    def update_from_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Update context after an LLM call."""
        self.last_context_size = prompt_tokens
        self.cumulative_prompt_tokens += prompt_tokens
        self.cumulative_completion_tokens += completion_tokens
        self.turn_count += 1

    def get_context_size(self) -> int:
        """Return total context size (cumulative prompt + completion)."""
        return self.cumulative_prompt_tokens + self.cumulative_completion_tokens

    def reset(self) -> None:
        """Reset context (new session)."""
        self.cumulative_prompt_tokens = 0
        self.cumulative_completion_tokens = 0
        self.last_context_size = 0
        self.turn_count = 0


# Session-scoped context storage (module-level singleton for simplicity)
_contexts: dict[str, AgentContext] = {}


def get_context(session_id: str) -> AgentContext:
    """Get or create context for a session."""
    if session_id not in _contexts:
        _contexts[session_id] = AgentContext(session_id=session_id)
    return _contexts[session_id]


def update_context(
    session_id: str, prompt_tokens: int, completion_tokens: int
) -> AgentContext:
    """Update and return context for a session."""
    ctx = get_context(session_id)
    ctx.update_from_usage(prompt_tokens, completion_tokens)
    return ctx


def remove_context(session_id: str) -> None:
    """Remove session context (session ended)."""
    _contexts.pop(session_id, None)
