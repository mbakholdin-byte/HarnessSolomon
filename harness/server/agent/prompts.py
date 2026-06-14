"""System prompt construction for the agent loop (Шаг 6).

The system prompt is rebuilt per-session because the tool list is
considered stable (TOOL_SCHEMAS) but ``project_root`` is part of the
agent's environment and may be useful to the model for resolving
relative paths in tool arguments.

The prompt has three sections:
  1. Role and operating envelope (project_root, paths).
  2. Available tools — rendered from TOOL_SCHEMAS.
  3. Behavioural rules (no destructive bash, prefer minimal edits, ...).

The function is intentionally dependency-free apart from ``pathlib``
and ``TOOL_SCHEMAS`` so it can be unit-tested without spinning up the
whole stack.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


# === Constants ===

SYSTEM_PROMPT: str = (
    "You are Solomon, an AI agent working under a sandboxed project_root.\n"
    "\n"
    "Operating rules:\n"
    "  - All file paths in tool arguments are interpreted relative to "
    "project_root (or absolute paths under it). Paths outside project_root "
    "are refused by the runtime.\n"
    "  - You MUST NOT issue destructive shell commands. The runtime will "
    "refuse: 'rm -rf /', 'rm -rf ~', 'del /s', 'format *', 'git push --force', "
    "'git reset --hard', and similar patterns.\n"
    "  - Prefer the smallest change that solves the task. Re-read files "
    "after editing them to confirm the result.\n"
    "  - When the user asks a question you can answer from existing "
    "context, answer directly without invoking a tool.\n"
    "  - When you must call a tool, make exactly one call at a time and "
    "wait for the result before proceeding.\n"
    "  - Stop as soon as the task is complete. Do not invent follow-up "
    "work.\n"
    "\n"
    "Output format:\n"
    "  - Reply in plain text. Use Markdown for structure when it helps.\n"
    "  - Keep tool calls to a minimum; avoid redundant reads.\n"
)


def _format_tool(tool: dict[str, Any]) -> str:
    """Render one tool entry in a human-readable block."""
    name = tool.get("name", "<unnamed>")
    desc = (tool.get("description") or "").strip()
    params = tool.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])

    lines = [f"### {name}", ""]
    if desc:
        lines.append(desc)
        lines.append("")
    if props:
        lines.append("Parameters:")
        for pname, spec in props.items():
            marker = " (required)" if pname in required else " (optional)"
            pdesc = (spec.get("description") or "").strip()
            ptype = spec.get("type", "any")
            lines.append(f"  - `{pname}`: {ptype}{marker} — {pdesc}")
        lines.append("")
    return "\n".join(lines)


def build_system_prompt(project_root: Path, tools: list[dict]) -> str:
    """Build a complete system prompt for one agent session.

    Args:
        project_root: Resolved project root for this session. The model
            uses this to reason about relative paths.
        tools: Tool schemas to advertise to the model. In production this
            is ``TOOL_SCHEMAS`` from ``harness.server.agent.tools``.

    Returns:
        A single string suitable for use as a system message.
    """
    root_str = str(project_root.resolve(strict=False))
    tools_section = "\n".join(_format_tool(t) for t in tools) if tools else "(no tools available)"
    return (
        f"{SYSTEM_PROMPT}\n"
        f"project_root: {root_str}\n"
        f"\n"
        f"Available tools:\n"
        f"\n"
        f"{tools_section}"
    )


__all__ = ["SYSTEM_PROMPT", "build_system_prompt"]
