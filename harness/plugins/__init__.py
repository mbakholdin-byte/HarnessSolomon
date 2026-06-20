"""Phase 6.2A v1.27.0 — PluginRegistry singleton + public API.

The :class:`PluginRegistry` is the in-memory store for plugin-provided
hooks, tools, and scopes. It is a singleton (one per process) accessed
via :func:`get_registry` / :func:`reset_registry`.

Trust boundary (CRITICAL):

    Plugin code is loaded via ``importlib`` with a RESTRICTED module
    global namespace (see :mod:`harness.plugins.loader`). The registry
    deliberately exposes NO references to ``harness.agents.*`` or
    ``harness.server.*`` — only the registration surface below. An AST
    test in ``tests/test_plugin_loader_v127.py`` enforces that
    ``harness/plugins/*.py`` does not ``import harness.agents`` or
    ``harness.server``.

The registry is intentionally synchronous and dumb: it stores callables
and metadata; it does NOT dispatch them (that responsibility belongs to
the Phase 4.0 hooks runner when wiring is added in a later step).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from harness.plugins.base import Plugin

__all__ = [
    "Plugin",
    "PluginInfo",
    "PluginRegistry",
    "get_registry",
    "reset_registry",
]


@dataclass(frozen=True)
class PluginInfo:
    """Metadata snapshot for a loaded plugin.

    Returned by :meth:`PluginRegistry.list_plugins` and by the loader.
    Frozen so callers can safely cache / hash it.
    """

    name: str
    version: str
    source_path: str
    hooks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)


# Handler signature: takes a single event dict, returns None (fire-and-
# forget). The Phase 4.0 hooks runner has its own richer type; this is
# the minimal contract the registry enforces.
HookHandler = Callable[[dict[str, Any]], None]
# Tool handler: takes arbitrary kwargs, returns Any. Schema is a
# JSON-schema-ish dict validated lazily by the tool dispatcher (later).
ToolHandler = Callable[..., Any]


class PluginRegistry:
    """Singleton registry for plugin-provided hooks / tools / scopes.

    Not thread-safe (single-threaded startup contract — the loader runs
    in the lifespan handler before the event loop accepts traffic).
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[tuple[str, HookHandler]]] = {}
        self._tools: dict[str, tuple[ToolHandler, dict[str, Any]]] = {}
        self._scopes: dict[str, str] = {}
        self._plugins: dict[str, PluginInfo] = {}

    # === Registration surface ===

    def register_hook(
        self,
        hook_name: str,
        handler: HookHandler,
        plugin_name: str = "<anonymous>",
    ) -> None:
        """Register a hook handler for ``hook_name``.

        Multiple handlers per hook are allowed (append order preserved).
        """
        self._hooks.setdefault(hook_name, []).append((plugin_name, handler))

    def register_tool(
        self,
        tool_name: str,
        handler: ToolHandler,
        schema: dict[str, Any] | None = None,
        plugin_name: str = "<anonymous>",
    ) -> None:
        """Register a tool handler with an optional JSON-schema."""
        self._tools[tool_name] = (handler, schema or {})

        # Back-reference into the originating plugin's info, if known.
        info = self._plugins.get(plugin_name)
        if info is not None and tool_name not in info.tools:
            info.tools.append(tool_name)

    def register_scope(
        self,
        scope_name: str,
        description: str = "",
        plugin_name: str = "<anonymous>",
    ) -> None:
        """Register a named scope (used by the auth layer later)."""
        self._scopes[scope_name] = description

    def register_plugin(self, info: PluginInfo) -> None:
        """Record that a plugin was loaded (called by the loader)."""
        self._plugins[info.name] = info

    # === Introspection surface ===

    def list_plugins(self) -> list[PluginInfo]:
        """Return all loaded plugin infos (insertion order)."""
        return list(self._plugins.values())

    def hooks_for(self, hook_name: str) -> list[HookHandler]:
        """Return all handlers registered for ``hook_name`` (insertion order)."""
        return [h for _, h in self._hooks.get(hook_name, [])]

    def get_tool(self, tool_name: str) -> tuple[ToolHandler, dict[str, Any]] | None:
        """Return ``(handler, schema)`` for ``tool_name`` or None."""
        return self._tools.get(tool_name)

    def list_scopes(self) -> dict[str, str]:
        """Return a copy of the registered scopes."""
        return dict(self._scopes)

    def __len__(self) -> int:
        return len(self._plugins)


# === Singleton ===

_REGISTRY: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    """Return the process-wide :class:`PluginRegistry` singleton."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = PluginRegistry()
    return _REGISTRY


def reset_registry() -> None:
    """Reset the singleton (TEST-ONLY — clears all registrations)."""
    global _REGISTRY
    _REGISTRY = PluginRegistry()
