"""Solomon Harness — ``SessionLifecycle`` (Phase 3 v1.4.0).

A small async context manager that owns the **end-of-session hook**.
When the ``async with`` block exits (CLI run, WS disconnect, API
session close) the lifecycle fires a single ``ReflectionLoop.reflect``
call with the accumulated ``SessionEvent``s.

Design notes
------------
* **Fail-open.** A failure inside the reflection call (timeout, JSON
  parse error, T1+T2 both down, scratchpad write error) is logged
  via ``logger.warning`` and swallowed. The lifecycle is a *side
  effect* of the session — losing it must never break the user-facing
  response.
* **Per-call timeout.** The reflection call is wrapped in
  ``asyncio.wait_for(..., timeout=reflection_max_ms/1000)``. Even if
  the LLM is hung we close the session in bounded time.
* **Trust boundary.** The lifecycle never *imports* the concrete
  ``ReflectionLoop`` class. It reads the active reflection handle via
  ``getattr(self.runtime, "_reflection", None)`` so the runner / agent
  loop can be constructed in tests where the reflection module is
  not importable. This mirrors the v1.3.1 ``_tool_offloader`` pattern
  (``runtime.py:123-130``).
* **Stateless enter / stateful exit.** ``__aenter__`` simply stores
  the references and returns ``self``. ``__aexit__`` is where the work
  happens, so the construction of the context is cheap and exception
  safe.

Usage
-----

    async with SessionLifecycle(
        runtime=runtime,
        events=events_collector,
        settings=settings,
        audit=scratchpad_audit,
    ) as lifecycle:
        # ... run the session ...
        pass
    # on exit, reflection fires (best-effort)
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    pass

logger = logging.getLogger(__name__)


class SessionLifecycle:
    """Async context manager that fires ``ReflectionLoop`` on exit.

    Parameters
    ----------
    runtime:
        The :class:`ToolRuntime` (or anything with a ``_reflection``
        attribute). ``None`` is allowed — in that case the lifecycle
        is a no-op.
    events:
        A mutable list of ``SessionEvent``s collected by ``AgentLoop``
        during the session. ``None`` is allowed (no events collected
        → reflection is skipped).
    settings:
        Harness settings object. Reads ``reflection_enabled`` and
        ``reflection_max_ms``. If ``reflection_enabled`` is ``False``
        the exit hook is a no-op.
    audit:
        Optional ``ScratchpadAudit`` for structured logging. ``None``
        is allowed (audit is best-effort).
    """

    def __init__(
        self,
        *,
        runtime: Any,
        events: list[Any] | None,
        settings: Any,
        audit: Any = None,
    ) -> None:
        self._runtime = runtime
        self._events = events if events is not None else []
        self._settings = settings
        self._audit = audit

    async def __aenter__(self) -> "SessionLifecycle":
        """No-op enter. We only need to set up the context manager."""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Fire reflection on session end (best-effort, fail-open)."""
        # Read settings defensively — settings may not have the field
        # in old test fixtures.
        if not getattr(self._settings, "reflection_enabled", False):
            return

        # Read the reflection handle via getattr (trust boundary).
        reflection = getattr(self._runtime, "_reflection", None)
        if reflection is None:
            return

        # Nothing to reflect on.
        if not self._events:
            return

        # Compute timeout once.
        max_ms = getattr(self._settings, "reflection_max_ms", 10000)
        timeout_s = max_ms / 1000.0

        try:
            await asyncio.wait_for(
                reflection.reflect(self._events),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "SessionLifecycle: reflection timed out after %dms",
                max_ms,
            )
            self._safe_audit("reflection_timeout", {"max_ms": max_ms})
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("SessionLifecycle: reflection failed: %s", exc)
            self._safe_audit("reflection_failed", {"error": str(exc)})

    def _safe_audit(self, event: str, payload: dict[str, Any]) -> None:
        """Record an audit event if audit is wired; swallow errors."""
        if self._audit is None:
            return
        try:
            record = getattr(self._audit, "record", None)
            if record is None:
                return
            record(event=event, **payload)
        except Exception:  # noqa: BLE001
            # Audit is best-effort.
            pass
