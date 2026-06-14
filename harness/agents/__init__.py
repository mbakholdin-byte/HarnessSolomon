"""Solomon Harness sub-agents (Phase 2.0).

Sub-agents are isolated, role-specific LLM loops that run inside their own
``git worktree`` and are dispatched by an LLM-as-router. Four built-in agents
ship with the package:

- ``explore`` — read-only repository reconnaissance
- ``plan``    — read-only plan generation
- ``code``    — full read/write access, smallest-change discipline
- ``review``  — read-only diff review

Public API (re-exported for convenience):

- :class:`AgentSpec`         — ``harness.agents.spec``
- :func:`parse_agent_md`     — ``harness.agents.spec``
- :func:`load_agent`         — ``harness.agents.registry``
- :func:`list_agents`        — ``harness.agents.registry``
- :func:`all_specs`          — ``harness.agents.registry``
- :class:`WorktreeSession`   — ``harness.agents.worktree``
- :class:`AgentRunner`       — ``harness.agents.runner``
- :class:`LLMRouterClassifier` — ``harness.agents.router``
- :class:`AdversarialVerify` — ``harness.agents.verify``
- :class:`MergeQueue`        — ``harness.agents.merge_queue``
"""
from __future__ import annotations

__all__ = [
    "AgentSpec",
    "parse_agent_md",
    "load_agent",
    "list_agents",
    "all_specs",
    "WorktreeSession",
    "WorktreeInfo",
    "AgentRunner",
    "LLMRouterClassifier",
    "RouterDecision",
    "AdversarialVerify",
    "MergeQueue",
    "MergeJob",
    "MergeResult",
]
