"""Phase 6.3 v1.28.0 — PluginDispatcher: bridge registry hooks to HookRunner.

The :class:`PluginDispatcher` subscribes to a :class:`HookRunner` (or is
called explicitly by ``safe_fire``) and, for each event, invokes every
callback that plugins have registered with the :class:`PluginRegistry`
under the matching hook name.

Design notes
------------

* The dispatcher is **in-process** — plugin callbacks share the event
  loop with the harness. Plugins are therefore TRUSTED code; the
  ``plugins_allowed`` whitelist is the primary defence.
* Failures in individual plugin callbacks are caught and logged. A
  crashing plugin MUST NEVER bring down the harness — the remaining
  callbacks for the same event still run and the runner continues.
* The dispatcher is intentionally cheap to construct and side-effect
  free until :meth:`subscribe_all` is called. Tests can build a bare
  dispatcher and call :meth:`dispatch` directly without subscribing.

Trust boundary (CRITICAL)
-------------------------

This module imports ONLY from :mod:`harness.plugins` (the registry)
and :mod:`harness.hooks.runner` (the runner type). It MUST NOT import
``harness.agents`` or ``harness.server`` — an AST test in
``tests/test_plugin_dispatch_v128.py`` enforces this.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from harness.plugins import PluginRegistry

if TYPE_CHECKING:  # pragma: no cover — typing only
    from harness.hooks.runner import HookRunner

logger = logging.getLogger(__name__)


class PluginDispatcher:
    """Bridges :class:`PluginRegistry` hooks to :class:`HookRunner` events.

    Plugins register callbacks via
    ``PluginRegistry.register_hook(event_name, callback)``. This
    dispatcher subscribes to the runner and invokes those callbacks
    in-process when the corresponding event fires.

    Example::

        registry = PluginRegistry.get()
        runner = HookRunner(hook_registry, default_timeout_ms=3000)
        dispatcher = PluginDispatcher(registry, runner)
        dispatcher.subscribe_all()

        # Plugin callback:
        def on_tool(event: dict) -> None:
            print(event["tool_name"])

        registry.register_hook("OnToolUse", on_tool, plugin_name="logger")

    Attributes:
        registry: The :class:`PluginRegistry` providing callbacks.
        runner: The :class:`HookRunner` the dispatcher subscribes to
            (may be ``None`` for tests that call :meth:`dispatch`
            directly).
        enabled: When ``False``, :meth:`dispatch` is a no-op. Mirrors
            ``settings.plugins_dispatch_enabled``.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        runner: "HookRunner | None",
        *,
        enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._enabled = enabled
        # Track which event names we've subscribed to on the runner so
        # we don't double-subscribe if :meth:`subscribe_all` is called
        # twice.
        self._subscribed: set[str] = set()

    @property
    def registry(self) -> PluginRegistry:
        """The bound :class:`PluginRegistry`."""
        return self._registry

    @property
    def runner(self) -> "HookRunner | None":
        """The bound :class:`HookRunner` (may be ``None`` in tests)."""
        return self._runner

    @property
    def enabled(self) -> bool:
        """Whether :meth:`dispatch` actually invokes callbacks."""
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        """Toggle the dispatcher at runtime (no re-subscription needed)."""
        self._enabled = value

    async def dispatch(
        self,
        event_name: str,
        payload: dict[str, Any],
    ) -> list[Any]:
        """Invoke every plugin callback registered for ``event_name``.

        Callbacks are invoked sequentially in registration order. A
        callback that raises is logged at WARNING level and skipped;
        the remaining callbacks still run. The harness NEVER crashes
        due to a plugin callback failure.

        Args:
            event_name: Hook name (e.g. ``"OnToolUse"``, matching the
                string passed to ``PluginRegistry.register_hook``).
            payload: Event payload dict. The same dict is passed to
                every callback; callbacks MUST NOT mutate it (they
                receive a shallow copy to enforce isolation).

        Returns:
            List of callback return values, in registration order.
            Failed callbacks contribute ``None`` to the list (their
            return value is undefined). When the dispatcher is
            disabled (``enabled=False``) or the event name is empty,
            an empty list is returned and no callbacks run.
        """
        if not self._enabled:
            return []
        if not event_name:
            return []

        handlers = self._registry.hooks_for(event_name)
        if not handlers:
            return []

        # Shallow-copy the payload so a plugin cannot mutate the
        # caller's dict (defence in depth — the runner's aggregate
        # uses its own final_payload, but in-process callbacks share
        # the reference by default).
        safe_payload = dict(payload)

        results: list[Any] = []
        for idx, handler in enumerate(handlers):
            try:
                result = handler(safe_payload)
            except Exception as exc:  # noqa: BLE001 — plugin code is untrusted
                logger.warning(
                    "plugin_dispatch: handler #%d for %s raised "
                    "%s: %s — continuing with remaining handlers",
                    idx,
                    event_name,
                    type(exc).__name__,
                    exc,
                )
                results.append(None)
                continue
            results.append(result)
        return results

    def subscribe_all(self) -> None:
        """Subscribe the dispatcher to every known event on the runner.

        Iterates over all :class:`EventType` members and calls
        :meth:`_subscribe_one` for each. Idempotent: calling twice
        does not double-subscribe (tracked via :attr:`_subscribed`).

        When ``self._runner`` is ``None`` (test mode), this method
        records the known event names but performs no subscription —
        :meth:`dispatch` can still be called directly.
        """
        from harness.hooks.events import EventType

        for event in EventType:
            self._subscribe_one(event.value)

    def _subscribe_one(self, event_name: str) -> None:
        """Record that we would dispatch ``event_name``.

        The actual integration with :class:`HookRunner` is performed
        in :meth:`harness.hooks.runner.HookRunner.fire` /
        :func:`harness.hooks.runner.safe_fire` by consulting
        ``app.state.plugin_dispatcher`` (set in lifespan). This method
        is therefore mostly bookkeeping — it tracks which events the
        dispatcher is "live" for so introspection / tests can verify
        the wiring without firing a real event.

        Args:
            event_name: Hook name to track.
        """
        self._subscribed.add(event_name)

    @property
    def subscribed_events(self) -> frozenset[str]:
        """Read-only view of the event names the dispatcher is live for."""
        return frozenset(self._subscribed)


__all__ = ["PluginDispatcher"]
