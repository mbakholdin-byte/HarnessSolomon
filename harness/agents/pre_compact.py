"""Phase 3 v1.5.0 Step 4: PreCompactHook.

Async callback fired by :class:`~harness.context.compaction.ContextCompactor`
BEFORE ``_run_slow_path`` executes. Captures high-signal session
state (last N messages, plan step, hot L0, metadata) and persists
to :class:`~harness.memory.unified.UnifiedMemory` so the next session
(or the current session after resume) can restore context that the
compactor is about to throw away.

Why a pre-compact hook?
- The Anthropic context engineering playbook recommends
  "Manual compact" as a last-resort strategy: you must save the
  valuable context BEFORE you lose it.
- Without this hook, ``/compact`` truncates the conversation and
  the reflection loop (Phase 3 v1.4.0) only sees the compacted
  state — losing high-signal original messages, plan steps, and
  scratchpad L0 content.
- The hook is the **bridge** between in-flight state and the
  post-compact representation.

Design constraints (mirror ``ReflectionLoop`` v1.4.0):
- Pure async callable. ``ContextCompactor`` calls it with
  ``asyncio.wait_for(timeout=pre_compact_max_ms/1000)``.
- Fail-open: timeout / exception / ``memory=None`` → log + audit,
  return ``None`` (compaction proceeds anyway).
- Persist via :class:`harness.memory.unified.UnifiedMemory` with a
  namespaced tag ``#pre-compact-{session_id}`` to avoid collisions
  with ``_persist_summary`` (which uses ``#compact-{session_id}``).
- Configurable fields via ``settings.pre_compact_save_fields``
  (comma-separated subset of: ``messages_last_n``, ``plan_step``,
  ``hot_l0``, ``metadata``).

Out of scope (deferred to v1.6.0+):
- Restore-on-resume flow (Step 4 only saves; loading is Phase 4
  hooks territory).
- Cross-session handoff (the state stays in UnifiedMemory L1 by
  default; cross-session sharing is a v1.6.0+ feature).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = ["PreCompactHook", "PreCompactState"]

logger = logging.getLogger(__name__)

#: Number of recent messages to keep in the snapshot. Tuned to be
#: small enough to fit a 1-2KB L1 note (UnifiedMemory write budget)
#: but large enough to capture the immediate task context.
DEFAULT_MESSAGES_LAST_N: Final[int] = 5

#: Valid field names for ``settings.pre_compact_save_fields``.
VALID_SAVE_FIELDS: Final[frozenset[str]] = frozenset(
    {"messages_last_n", "plan_step", "hot_l0", "metadata"},
)


@dataclass(frozen=True)
class PreCompactState:
    """Snapshot of session state captured before compaction.

    Attributes:
        session_id:      Session this state belongs to.
        messages_last_n: Last ``DEFAULT_MESSAGES_LAST_N`` user /
                         assistant messages (excluding tool messages
                         to keep the payload small).
        plan_step:       Current scratchpad plan step text (from
                         ``scratchpad.read_notes("L1", tag="plan")``)
                         or empty string if no plan exists.
        hot_l0:          Scratchpad L0 snapshot (hot, frequently-
                         accessed notes) as a single concatenated
                         string, or empty string if L0 is empty.
        metadata:        Free-form metadata (turn count, tokens
                         used, last_compact_at, model name, etc.).
        captured_at:     ``time.monotonic()`` at capture time
                         (used for staleness comparison on resume).
        fields_included: Tuple of field names that were actually
                         included (controls which optional attrs
                         are populated by :class:`PreCompactHook`).
    """

    session_id: str
    messages_last_n: list[dict[str, Any]] = field(default_factory=list)
    plan_step: str = ""
    hot_l0: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    captured_at: float = 0.0
    fields_included: tuple[str, ...] = field(default_factory=tuple)


class PreCompactHook:
    """Default pre-compact hook implementation.

    Captures state from the live ``messages`` list + optional
    scratchpad store, and persists the resulting
    :class:`PreCompactState` to :class:`UnifiedMemory` as an L1 note
    tagged ``#pre-compact-{session_id}``.

    Args:
        memory:   UnifiedMemory instance for the write. ``None``
                  disables persistence (capture still returns a
                  :class:`PreCompactState` for the in-process caller).
        settings: Settings instance. Reads
                  ``settings.pre_compact_save_fields`` to decide
                  which fields to include.
        audit:    Optional audit sink. Receives events
                  ``pre_compact_state_saved`` / ``pre_compact_timeout``
                  / ``pre_compact_failed``. Fail-open at audit
                  boundary (never raises).
        scratchpad: Optional scratchpad store for reading ``plan_step``
                    (from ``read_notes("L1", tag="plan")``) and
                    ``hot_l0`` (from ``read_notes("L0")``). ``None``
                    → both fields are empty strings.

    Example:
        >>> hook = PreCompactHook(memory=um, settings=settings)
        >>> state = await hook(
        ...     session_id="sess-42",
        ...     messages=current_messages,
        ...     metadata={"turn_count": 7, "tokens": 12000},
        ... )
        >>> state.session_id
        'sess-42'
        >>> "#pre-compact-sess-42" in state.metadata.get("tags", [])
        True
    """

    def __init__(
        self,
        memory: Any | None,
        settings: Any,
        *,
        audit: Any | None = None,
        scratchpad: Any | None = None,
    ) -> None:
        self._memory = memory
        self._settings = settings
        self._audit = audit
        self._scratchpad = scratchpad

    async def __call__(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> PreCompactState | None:
        """Capture state + persist to UnifiedMemory.

        Returns:
            The captured :class:`PreCompactState` on success.
            ``None`` on any failure (caller should proceed with
            compaction; the hook is best-effort).

        Notes:
            - The actual L1 write is best-effort and never raises
              (try/except + logger.warning). A failed write still
              returns the in-memory state to the caller.
            - Field inclusion is driven by
              ``settings.pre_compact_save_fields`` (comma-separated).
              Empty / unknown field names are silently skipped.
        """
        if not session_id:
            return None
        fields_included = self._resolve_save_fields()
        try:
            state = self._capture(
                session_id=session_id,
                messages=messages,
                metadata=metadata or {},
                fields_included=fields_included,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open at capture
            self._safe_audit("pre_compact_failed", {"error": str(exc), "session_id": session_id})
            logger.warning("PreCompactHook capture failed for %s: %s", session_id, exc)
            return None

        # Best-effort persist. Always return state to caller even on
        # write failure (in-memory state is still useful for the
        # caller's audit / logging).
        if self._memory is not None and "metadata" in fields_included:
            try:
                await self._persist(state)
                self._safe_audit(
                    "pre_compact_state_saved",
                    {
                        "session_id": session_id,
                        "fields": list(fields_included),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — fail-open at write
                self._safe_audit(
                    "pre_compact_failed",
                    {"error": f"persist: {exc}", "session_id": session_id},
                )
                logger.warning(
                    "PreCompactHook persist failed for %s: %s", session_id, exc,
                )
        return state

    # --- internals ---

    def _resolve_save_fields(self) -> tuple[str, ...]:
        """Parse ``settings.pre_compact_save_fields`` into a tuple.

        Empty string → save nothing (hook is a no-op).
        Unknown field names are silently skipped.
        """
        raw = getattr(self._settings, "pre_compact_save_fields", "") or ""
        if not raw.strip():
            return ()
        out: list[str] = []
        seen: set[str] = set()
        for token in raw.split(","):
            token = token.strip()
            if not token or token not in VALID_SAVE_FIELDS or token in seen:
                continue
            seen.add(token)
            out.append(token)
        return tuple(out)

    def _capture(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any],
        fields_included: tuple[str, ...],
    ) -> PreCompactState:
        """Build a :class:`PreCompactState` from the live inputs."""
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "captured_at": time.monotonic(),
            "fields_included": fields_included,
        }
        if "messages_last_n" in fields_included:
            kwargs["messages_last_n"] = self._extract_last_n(messages)
        else:
            kwargs["messages_last_n"] = []
        if "plan_step" in fields_included:
            kwargs["plan_step"] = self._read_plan_step()
        else:
            kwargs["plan_step"] = ""
        if "hot_l0" in fields_included:
            kwargs["hot_l0"] = self._read_hot_l0()
        else:
            kwargs["hot_l0"] = ""
        if "metadata" in fields_included:
            kwargs["metadata"] = dict(metadata)
        else:
            kwargs["metadata"] = {}
        return PreCompactState(**kwargs)

    @staticmethod
    def _extract_last_n(
        messages: list[dict[str, Any]],
        n: int = DEFAULT_MESSAGES_LAST_N,
    ) -> list[dict[str, Any]]:
        """Last N user/assistant messages (skip tool messages for size)."""
        filtered = [m for m in messages if m.get("role") in ("user", "assistant")]
        return list(filtered[-n:])

    def _read_plan_step(self) -> str:
        """Read current plan_step from scratchpad L1 (tag='plan')."""
        if self._scratchpad is None:
            return ""
        try:
            notes = self._scratchpad.read_notes("L1", tag="plan", limit=1)
        except Exception:  # noqa: BLE001 — fail-open
            return ""
        if not notes:
            return ""
        note = notes[0]
        return getattr(note, "content", "") or ""

    def _read_hot_l0(self) -> str:
        """Read scratchpad L0 (hot notes) as a single string."""
        if self._scratchpad is None:
            return ""
        try:
            notes = self._scratchpad.read_notes("L0", limit=10)
        except Exception:  # noqa: BLE001 — fail-open
            return ""
        if not notes:
            return ""
        parts: list[str] = []
        for note in notes:
            content = getattr(note, "content", "") or ""
            if content:
                parts.append(content)
        return "\n".join(parts)

    async def _persist(self, state: PreCompactState) -> None:
        """Write the state to UnifiedMemory L1 with namespaced tag.

        Uses :meth:`UnifiedMemory.write` (the only public write API).
        The tag ``#pre-compact-{session_id}`` namespaces the note
        separately from ``#compact-{session_id}`` written by
        ``ContextCompactor._persist_summary`` (Phase 3 v1.0.0+).
        """
        content = self._format_state(state)
        tag = f"pre-compact-{state.session_id}"
        await self._memory.write(
            text=content,
            tags=[tag, "#pre-compact", f"#session/{state.session_id}"],
            metadata={
                "session_id": state.session_id,
                "captured_at": state.captured_at,
                "fields": list(state.fields_included),
                "kind": "pre_compact",
            },
        )

    @staticmethod
    def _format_state(state: PreCompactState) -> str:
        """Render state as a compact L1 note body (markdown-ish)."""
        lines: list[str] = [
            f"# pre-compact snapshot — {state.session_id}",
            f"captured_at: {state.captured_at:.3f}",
            f"fields: {','.join(state.fields_included) or '(none)'}",
            "",
        ]
        if "messages_last_n" in state.fields_included and state.messages_last_n:
            lines.append("## last messages")
            for m in state.messages_last_n:
                role = m.get("role", "?")
                content = (m.get("content") or "")[:300]
                lines.append(f"- [{role}] {content}")
            lines.append("")
        if "plan_step" in state.fields_included and state.plan_step:
            lines.append("## plan")
            lines.append(state.plan_step)
            lines.append("")
        if "hot_l0" in state.fields_included and state.hot_l0:
            lines.append("## hot L0")
            lines.append(state.hot_l0[:2000])  # cap to 2KB
            lines.append("")
        if "metadata" in state.fields_included and state.metadata:
            lines.append("## metadata")
            for k, v in sorted(state.metadata.items()):
                lines.append(f"- {k}: {v}")
        return "\n".join(lines)

    def _safe_audit(self, event: str, payload: dict[str, Any]) -> None:
        """Emit audit event, never raise (mirror PrivacyZoneFilter)."""
        if self._audit is None:
            return
        try:
            record = getattr(self._audit, "record", None)
            if record is None:
                return
            try:
                record(event, payload)
            except TypeError:
                record(event=event, **payload)
        except Exception as exc:  # noqa: BLE001 — audit MUST fail-open
            logger.warning("PreCompactHook audit failed: %s", exc)
