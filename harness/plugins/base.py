"""Phase 6.2A v1.27.0 — Plugin base class.

Minimal abstract base for plugins. A plugin is a unit of code that
self-registers hooks / tools / scopes with the
:class:`~harness.plugins.PluginRegistry` via its ``register`` method.

The base class is intentionally small (~40 LoC): it declares metadata
fields (``name`` / ``version``) and the ``register`` contract. The
registry does NOT subclass ``Plugin`` — concrete plugins either subclass
``Plugin`` (preferred) or expose a module-level ``register(registry)``
function (the loader supports both forms).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from harness.plugins import PluginRegistry


class Plugin:
    """Abstract base for Harness plugins.

    Concrete plugins subclass this and override :meth:`register`.

    Example::

        class LoggerPlugin(Plugin):
            name = "logger"
            version = "1.0.0"

            def register(self, registry: PluginRegistry) -> None:
                registry.register_hook("OnToolUse", self._on_tool_use)

            def _on_tool_use(self, event: dict[str, Any]) -> None:
                import sys
                print(f"[logger] {event}", file=sys.stderr)

    Alternatively, a plugin module may expose a module-level
    ``register(registry)`` function (duck typing) — the loader accepts
    either form.
    """

    #: Human-readable plugin name. MUST be set by subclasses.
    name: str = ""

    #: SemVer-ish version string. MUST be set by subclasses.
    version: str = "0.0.0"

    def register(self, registry: PluginRegistry) -> None:
        """Register hooks / tools / scopes with ``registry``.

        Default implementation is a no-op. Subclasses override this
        to perform their actual self-registration.

        Args:
            registry: The :class:`PluginRegistry` singleton to register
                with. The plugin MUST NOT retain a reference to it
                beyond this call (the registry may be replaced / reset
                at runtime).
        """
        _ = registry  # noqa: F841 — intentional no-op default
        return None
