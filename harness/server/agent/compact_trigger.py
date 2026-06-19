"""Solomon Harness — ``CompactTrigger`` (Phase 3 v1.4.0).

Manual /compact trigger. Wraps :class:`ContextCompactor` with
explicit per-call timeout, audit, and a thin interface that the
CLI subcommand, HTTP route, and WebSocket message handler can
all share.

Why a wrapper?
--------------
The underlying ``ContextCompactor.force_compact`` already does the
right thing (skips threshold, runs slow path, returns
``CompactResult``). What it does *not* do:

* Enforce a per-call timeout (the plan budget for a manual compact
  is a setting, not a hard deadline).
* Emit a ``manual_compact`` audit event with the result.
* Handle the "messages not yet loaded" / "compactor unavailable"
  cases that a public-facing trigger has to answer.

``CompactTrigger`` fills those three gaps. It is intentionally
tiny — the value is in the audit + timeout + interface, not in
re-implementing compact logic.

Trust boundary
--------------
``runner.py`` does NOT import this module (verified by
``test_runner_does_not_import_compact_trigger``). The HTTP route
imports it directly, the CLI imports it directly, and the
WebSocket message handler imports it directly. Only the runner
is part of the trust boundary; the trigger is a small enough
unit that pulling it in via the trigger module is fine.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from harness.hooks.runner import safe_fire  # Phase 4.13A v1.23.0: OnCompaction hook

if TYPE_CHECKING:  # pragma: no cover
    from harness.context.compaction import CompactResult

logger = logging.getLogger(__name__)


class CompactTrigger:
    """Manual /compact trigger.

    Parameters
    ----------
    compactor:
        The :class:`ContextCompactor` instance. When ``None`` the
        trigger is a no-op and returns ``None`` from
        :meth:`compact_now`.
    settings:
        Harness settings object. Reads ``manual_compact_max_ms``
        (per-call timeout in milliseconds). Defaults to 30 s.
    audit:
        Optional audit writer. ``None`` disables audit events.
    """

    def __init__(
        self,
        compactor: Any | None,
        settings: Any,
        *,
        audit: Any | None = None,
    ) -> None:
        self._compactor = compactor
        self._settings = settings
        self._audit = audit

    async def compact_now(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        session_id: str,
        bypass_cache: bool = False,
    ) -> "CompactResult | None":
        """Force-compact a session's context.

        Returns the :class:`CompactResult` on success, or ``None`` when
        the compactor is unavailable / a timeout occurs / a hard
        error happens. Errors are logged and audited but never
        propagate — ``/compact`` is a side-effect, not a gate.

        The caller decides what to do with the result (CLI prints a
        summary, HTTP returns 200 with JSON, WS sends ``compact_done``).
        """
        if self._compactor is None:
            logger.warning("CompactTrigger: compactor not available")
            self._safe_audit("compact_unavailable", {"session_id": session_id})
            return None

        # Compute timeout once.
        max_ms = int(
            getattr(self._settings, "manual_compact_max_ms", 30_000) or 30_000,
        )
        timeout_s = max_ms / 1000.0

        try:
            result = await asyncio.wait_for(
                self._compactor.force_compact(
                    messages,
                    model,
                    session_id=session_id,
                    bypass_cache=bypass_cache,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "CompactTrigger: force_compact timed out after %dms", max_ms,
            )
            self._safe_audit(
                "compact_timeout",
                {"session_id": session_id, "max_ms": max_ms},
            )
            return None
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("CompactTrigger: force_compact failed: %s", exc)
            self._safe_audit(
                "compact_failed",
                {"session_id": session_id, "error": str(exc)},
            )
            return None

        self._safe_audit(
            "manual_compact",
            {
                "session_id": session_id,
                "original_tokens": result.original_tokens,
                "compacted_tokens": result.compacted_tokens,
                "saved_tokens": result.saved_tokens,
                "cache_hit": result.cache_hit,
            },
        )
        # Phase 4.13A v1.23.0: OnCompaction hook. Hot-path safe_fire —
        # fired AFTER the audit entry so a hook ``block`` decision is
        # purely advisory (the compact already ran). The payload matches
        # the Phase 4.13A spec:
        # ``{session_id, agent_id, pre_tokens, post_tokens, ratio,
        #    trigger_reason}`` plus the schema-required
        # ``summary_preview`` / ``saved_tokens`` so advisory schema
        # validation in ``OnCompactionPayload`` passes.
        pre_tokens = int(result.original_tokens)
        post_tokens = int(result.compacted_tokens)
        ratio = (
            post_tokens / pre_tokens
            if pre_tokens > 0
            else 0.0
        )
        try:
            await safe_fire(
                "OnCompaction",
                session_id=session_id,
                agent_id="",
                payload={
                    # Phase 4.13A spec fields.
                    "session_id": session_id,
                    "agent_id": "",
                    "pre_tokens": pre_tokens,
                    "post_tokens": post_tokens,
                    "ratio": round(ratio, 4),
                    "trigger_reason": (
                        "manual" if not bypass_cache else "manual_bypass_cache"
                    ),
                    # Schema-required fields (OnCompactionPayload).
                    "summary_preview": (
                        result.summary_preview[:200]
                        if result.summary_preview
                        else ""
                    ),
                    "saved_tokens": int(result.saved_tokens),
                    # Diagnostic.
                    "cache_hit": bool(result.cache_hit),
                },
            )
        except Exception:  # noqa: BLE001 — hook failure must never break compact
            logger.debug(
                "OnCompaction safe_fire failed for session=%s",
                session_id,
                exc_info=True,
            )
        return result

    def _safe_audit(self, event: str, payload: dict[str, Any]) -> None:
        """Record an audit event if audit is wired; swallow errors."""
        if self._audit is None:
            return
        try:
            record = getattr(self._audit, "record", None)
            if record is None:
                return
            record(event=event, **payload)
        except Exception:  # noqa: BLE001 — audit is best-effort
            pass
