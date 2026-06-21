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
from harness.plugins.manifest_v2 import PluginManifestV2

__all__ = [
    "Plugin",
    "PluginInfo",
    "PluginManifestV2",
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
        self._disabled: set[str] = set()

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
        # If it was previously disabled, clear that state on re-load.
        self._disabled.discard(info.name)

    # === Lifecycle surface (v1.31.0) ===

    def enable(self, name: str) -> bool:
        """Re-enable a previously disabled plugin.

        Removes ``name`` from the disabled set. The plugin must be
        reloaded via the loader for its hooks/tools to become active
        again — this method only clears the administrative block.
        Returns True if the plugin was previously disabled, False
        if it was already active or unknown.
        """
        if name in self._disabled:
            self._disabled.discard(name)
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a plugin: unload + mark disabled.

        Removes the plugin from ``_plugins``, its hooks from
        ``_hooks``, and its tools from ``_tools``. Adds ``name``
        to the ``_disabled`` set so it cannot be re-loaded until
        ``enable()`` is called. Returns True if the plugin was
        loaded, False if it was already disabled or unknown.
        """
        if name not in self._plugins:
            return False
        # Remove the plugin record.
        info = self._plugins.pop(name)
        # Remove its hooks.
        for hook_name in list(self._hooks.keys()):
            self._hooks[hook_name] = [
                (pn, h) for (pn, h) in self._hooks[hook_name]
                if pn != name
            ]
            if not self._hooks[hook_name]:
                del self._hooks[hook_name]
        # Remove its tools.
        for tool_name in list(info.tools):
            self._tools.pop(tool_name, None)
        # Mark disabled.
        self._disabled.add(name)
        return True

    def get_plugin(self, name: str) -> PluginInfo | None:
        """Return a single plugin by name, or None if not found."""
        return self._plugins.get(name)

    def is_disabled(self, name: str) -> bool:
        """Return True if the plugin name is in the disabled set."""
        return name in self._disabled

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
