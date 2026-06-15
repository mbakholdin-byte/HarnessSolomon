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
    {
        "name": "scratchpad_l2_search",
        "description": (
            "Phase 3 v1.3.0: search the long-term L2 archive of the "
            "scratchpad with a free-text query. Combines BM25 keyword "
            "match and dense-vector cosine similarity (Reciprocal Rank "
            "Fusion, k=60) and, when a curator LLM is available, "
            "re-ranks the top candidates. Use this when you need "
            "**older context** that isn't in the L0 hot layer (auto-"
            "injected) or the L1 plan. Returns a list of matching "
            "notes ordered by relevance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query (e.g. 'what did we decide about X?').",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of notes to return. Default 10.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "scratchpad_l2_promote_to_l1",
        "description": (
            "Phase 3 v1.3.0: fetch the top-N L2 notes that match the "
            "query, summarise them with the curator LLM, and write the "
            "summary as a fresh L1 plan note. Use this to surface "
            "recurring themes from the long-term archive into the "
            "session's working state. The new L1 note is returned so "
            "you can reference it in subsequent turns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query describing the theme to summarise.",
                },
                "max_notes": {
                    "type": "integer",
                    "description": "Maximum number of L2 notes to include in the summary. Default 20.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "scratchpad_read_offloaded",
        "description": (
            "Phase 3 v1.3.1: read a previously offloaded tool result "
            "by its note id. When a tool result exceeded the offload "
            "threshold (default 25 KB) the loop wrote the full body "
            "to L2 scratchpad and replaced the inline message with a "
            "stub that includes the note id and a 3-line preview. "
            "Use this tool to pull the full body when the preview "
            "isn't enough to make a decision. Returns up to "
            "``max_bytes`` characters of the offloaded content "
            "(default 4 KB)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "The note id returned in the offload stub header.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": (
                        "Maximum number of bytes (chars) to return. "
                        "Default ``settings.tool_offload_read_max_bytes`` "
                        "(4096). Pass a larger value to fetch the full "
                        "offloaded body in one call."
                    ),
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "scratchpad_search_offloaded",
        "description": (
            "Phase 3 v1.3.1: semantic search across offloaded tool "
            "results. Reuses the v1.3.0 ``L2Retriever`` (hybrid "
            "dense+BM25 with optional LLM-curator re-rank) but "
            "restricts the corpus to notes tagged ``#tool-offload`` "
            "(i.e. content that the loop has offloaded because the "
            "tool output exceeded the offload threshold). Use this "
            "to recover an earlier large tool result that you only "
            "remember by topic, not by note id. Returns a JSON list "
            "of ``{id, score, preview, tags}`` ordered by relevance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query describing the offloaded result you want.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default 5.",
                },
            },
            "required": ["query"],
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
