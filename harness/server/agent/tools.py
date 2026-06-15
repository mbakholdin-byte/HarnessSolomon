"""Tool schemas + in-memory registry (Шаг 4).

Schemas follow the OpenAI function-calling / Anthropic tool-use format:
each tool is a dict with ``name``, ``description``, and ``parameters``
(JSON Schema). The LLM layer (Phase 1) will emit these as the tool
specification for the model.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeAlias

# === JSON Schemas (for future LLM) ===

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a text file under project_root and return its contents. "
            "Paths outside project_root are refused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to project_root (or absolute under it).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file under project_root. "
            "Fails if old_string is not found (does not create the file)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path under project_root."},
                "old_string": {"type": "string", "description": "Exact substring to replace."},
                "new_string": {"type": "string", "description": "Replacement content."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file under project_root. "
            "Creates parent directories as needed. Overwrites existing files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path under project_root."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bash",
        "description": (
            "Run a shell command. Some dangerous commands (rm -rf /, del /s, "
            "format, git push --force, git reset --hard) are refused by safety. "
            "Timeout: 30s by default, configurable 1-300s."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (1-300, default 30).",
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search for a regex pattern in files. Uses ripgrep (rg) if available, "
            "falls back to grep -rn. Path must be under project_root."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "path": {
                    "type": "string",
                    "description": "Directory under project_root to search (default: project_root).",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (1-300, default 30).",
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob",
        "description": (
            "List files matching a glob pattern. Path must be under project_root. "
            "Examples: '**/*.py', 'docs/*.md'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern."},
                "path": {
                    "type": "string",
                    "description": "Base directory under project_root (default: project_root).",
                },
            },
            "required": ["pattern"],
        },
    },
    # === Phase 3 v1.2.0: Scratchpad (Write context) ===
    {
        "name": "scratchpad_write_note",
        "description": (
            "Persist a note to the per-(session, agent) scratchpad. "
            "Use 'L0' for hot facts that should appear in the system "
            "prompt on every turn (1KB cap, oldest auto-pruned). Use "
            "'L1' for plan / decision context. Use 'L2' for archive "
            "notes (dense+BM25 retrieval in v1.3.0)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["L0", "L1", "L2"],
                    "description": "Memory layer (L0=hot, L1=plan, L2=archive).",
                },
                "content": {
                    "type": "string",
                    "description": "Note text. Markdown-friendly.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for retrieval / grouping.",
                },
            },
            "required": ["level", "content"],
        },
    },
    {
        "name": "scratchpad_read_notes",
        "description": (
            "Read notes from the per-(session, agent) scratchpad, "
            "newest first. Filter by 'level' (L0/L1/L2) or omit to "
            "read all levels. Returns up to 50 notes with id, level, "
            "content, tags, created_at."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["L0", "L1", "L2"],
                    "description": "Filter by level (default: all).",
                },
            },
        },
    },
    {
        "name": "scratchpad_plan_step",
        "description": (
            "Add a step to the per-(session, agent) plan. Steps have a "
            "description, an optional list of dependency step ids, and a "
            "status lifecycle (pending → in_progress → done / blocked). "
            "Use deps to express ordering: this step waits on the listed "
            "step ids reaching 'done'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What this step accomplishes.",
                },
                "deps": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional list of plan_step ids this step depends on.",
                },
            },
            "required": ["description"],
        },
    },
    {
        "name": "scratchpad_mark_done",
        "description": (
            "Update the status of a plan step. Default status is 'done' "
            "but you can also set 'in_progress', 'blocked', or revert to "
            "'pending'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "step_id": {
                    "type": "integer",
                    "description": "Plan step id (from scratchpad_plan_step or scratchpad_read_notes).",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                    "description": "New status. Default 'done'.",
                },
            },
            "required": ["step_id"],
        },
    },
]


# === In-memory registry ===

#: A tool callable takes a dict of arguments and returns a dict payload
#: (or raises). The runtime normalises the result into a ToolResult.
ToolCallable: TypeAlias = Callable[[dict[str, Any]], Any]


class ToolRegistry:
    """Simple name → callable map.

    Phase 0 uses an in-memory dict. Phase 1+ may swap for a Redis-backed
    registry or plugin loader.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolCallable] = {}

    def register(self, name: str, fn: ToolCallable) -> None:
        """Register a callable under ``name``. Overwrites if already present."""
        self._tools[name] = fn

    def get(self, name: str) -> ToolCallable | None:
        """Return the callable for ``name`` or None."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """Return sorted list of registered tool names."""
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools


# A module-level registry (initially empty). The agent loop builds a
# per-session ToolRuntime and uses ``runtime.execute(name, args)`` directly;
# the registry here is exposed for future plugin/extension use.
#: Global in-memory tool registry. Empty by default — extend at startup.
registry: ToolRegistry = ToolRegistry()
