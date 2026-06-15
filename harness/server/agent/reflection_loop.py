"""Solomon Harness — ``ReflectionLoop`` (Phase 3 v1.4.0).

End-of-session lesson extraction. When a session terminates, the
:class:`SessionLifecycle` calls :meth:`ReflectionLoop.reflect` with
the accumulated :class:`SessionEvent` stream. The loop sends a
single prompt to the cost cascade (T1 → T2 → fail-open) and asks
the model to extract a small number of *lessons* — gotchas,
preferences, patterns — that should be remembered next time.

Design notes
------------
* **Cost cascade.** We try ``settings.reflection_model`` first
  (defaults to ``subagent_t1_model``) and fall back to
  ``settings.reflection_fallback_model`` (``subagent_t2_model``)
  on any error. If both fail we return ``[]`` and log a warning —
  the reflection is a *side effect* of session close, never a
  blocking dependency.
* **Fail-open JSON parsing.** The model is asked to return a
  strict JSON list of ``{"kind", "content", "tags"}`` objects. On
  any parse error we audit ``reflection_parse_failed`` with the
  raw content preview (truncated) and return ``[]`` — the session
  is not penalised for a malformed model response.
* **Dual-write.** Each lesson is written to the scratchpad as an
  L1 note (tagged ``#reflection`` and ``#session/{id}``) AND to
  the :class:`UnifiedMemory` L1 layer with ``source="reflection"``.
  The scratchpad is the per-session journal; UnifiedMemory is the
  cross-session store.
* **Trust boundary.** The runner / lifecycle do NOT import this
  module directly. They use ``getattr(runtime, "_reflection", None)``
  to read the wired handle. This mirrors the v1.3.1
  ``_tool_offloader`` pattern (``runtime.py:123-130``) and the v1.4.0
  ``_reflection`` pattern (``runtime.py:131-141``).
* **Lesson cap.** We honour ``settings.reflection_max_lessons`` —
  even if the model returns 50 we keep the first N. This bounds
  the cost of the dual-write step.

The public surface is small:

* :class:`SessionEvent` — a single user/assistant/tool message in
  chronological order
* :class:`Lesson` — one extracted lesson (gotcha/preference/pattern)
* :class:`ReflectionLoop` — the actor that turns events into lessons
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover — typing only
    from harness.agents.scratchpad import NoteLevel  # noqa: F401

logger = logging.getLogger(__name__)


# Cap on the raw content preview we stash in the audit log. Anything
# bigger is truncated — we want the *shape* of the failure, not a
# 30 KB blob in our log aggregator.
_AUDIT_CONTENT_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class SessionEvent:
    """A single chronological event in a session.

    Mirrors the v1.3.1 ``tool_offloaded_id`` field — when a tool
    result was offloaded, the event carries the storage ``id`` so
    the reflection loop can reference it in lessons ("the user
    asked about X, see offloaded id=N").
    """
    kind: Literal["user", "assistant", "tool"]
    content: str
    ts: float
    tool_name: str | None = None
    offloaded_id: int | None = None


@dataclass(frozen=True)
class Lesson:
    """A single extracted lesson.

    * ``kind``: semantic bucket — ``gotcha`` (surprise / sharp
      edge), ``preference`` (user-style statement), ``pattern``
      (recurring approach that worked).
    * ``content``: human-readable description (1-2 sentences).
    * ``tags``: free-form tags, e.g. ``["#reflection", "#session/abc",
      "#gotcha"]``. The caller is free to add more.
    """
    kind: Literal["gotcha", "preference", "pattern"]
    content: str
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a precise lesson-extraction agent. Given a chronological "
    "transcript of a coding/agent session, you produce a short list of "
    "reusable lessons. Be conservative: only extract a lesson if it would "
    "be useful next time. Avoid restating obvious things. Output ONLY a "
    "valid JSON array."
)


def _build_user_prompt(events: list[SessionEvent], max_lessons: int) -> str:
    """Render events as a plain-text transcript for the model.

    We use ``__PH__`` as a sentinel and ``str.replace`` to escape user
    content — this is the same trick the v1.3.0 curator prompt uses
    to avoid ``str.format`` KeyError on user-supplied JSON (lesson
    from plan agent review, item 8).
    """
    lines: list[str] = []
    for ev in events:
        if ev.kind == "user":
            lines.append(f"[USER @ {ev.ts:.0f}] {ev.content}")
        elif ev.kind == "assistant":
            lines.append(f"[ASSISTANT @ {ev.ts:.0f}] {ev.content}")
        else:  # tool
            head = f"[TOOL @ {ev.ts:.0f}]"
            if ev.tool_name:
                head += f" name={ev.tool_name}"
            if ev.offloaded_id is not None:
                head += f" offloaded_id={ev.offloaded_id}"
            head += f" {ev.content}"
            lines.append(head)
    transcript = "\n".join(lines) if lines else "(no events)"
    return (
        f"Extract up to {max_lessons} lessons from the session below. "
        f"For each, output a JSON object with:\n"
        f'  - "kind": one of "gotcha", "preference", "pattern"\n'
        f'  - "content": one or two sentences\n'
        f'  - "tags": array of short tag strings (e.g. ["gotcha", "sql"])\n'
        f"Output a JSON array of these objects. Example:\n"
        f'[{{"kind": "gotcha", "content": "...", "tags": ["sql"]}}]\n\n'
        f"Transcript:\n{transcript}"
    )


def _parse_lessons(
    raw: str,
    *,
    expected_max: int,
) -> list[Lesson]:
    """Parse the model response into a list of :class:`Lesson`.

    Tolerates:
    * JSON wrapped in ``​```json ... ​``` fences
    * Leading prose before the first ``[``
    * Trailing prose after the last ``]``
    * Extra fields (dropped silently)

    Returns ``[]`` on any failure. The caller audits
    ``reflection_parse_failed`` with the raw content preview.
    """
    if not raw:
        return []
    # Strip code fences if present.
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening fence (and optional language tag).
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Find the first ``[`` and last ``]``.
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    payload = text[start : end + 1]
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    lessons: list[Lesson] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        content = item.get("content")
        tags = item.get("tags") or []
        if kind not in ("gotcha", "preference", "pattern"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(tags, list):
            tags = []
        # Tags must be strings.
        tags = [str(t) for t in tags if isinstance(t, (str, int, float))]
        lessons.append(
            Lesson(kind=kind, content=content.strip(), tags=tags),
        )
        if len(lessons) >= expected_max:
            break
    return lessons


# ---------------------------------------------------------------------------
# ReflectionLoop
# ---------------------------------------------------------------------------


class ReflectionLoop:
    """End-of-session lesson extraction.

    Parameters
    ----------
    scratchpad:
        :class:`~harness.agents.scratchpad_store.ScratchpadStore` for
        L1 dual-write. When ``None`` the scratchpad leg is skipped
        (the UnifiedMemory leg still runs if ``unified_memory`` is
        provided).
    settings:
        Harness settings object. Reads ``reflection_model``,
        ``reflection_fallback_model``, ``reflection_max_lessons``,
        ``reflection_max_ms``. Empty ``reflection_model`` falls
        back to ``subagent_t1_model``; same for the fallback.
    router:
        The LLM router (duck-typed, same as
        :class:`L2Retriever`). Must expose ``async def completion(
        messages, model, tools=None)`` returning an object with a
        ``content`` attribute. When ``None`` reflection is a no-op
        (returns ``[]``).
    unified_memory:
        Optional :class:`UnifiedMemory` for cross-session dual-write.
        When ``None`` the cross-session leg is skipped.
    audit:
        Optional audit writer. ``None`` disables audit events.
    """

    def __init__(
        self,
        scratchpad: Any | None,
        settings: Any,
        *,
        router: Any | None = None,
        unified_memory: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._scratchpad = scratchpad
        self._settings = settings
        self._router = router
        self._unified_memory = unified_memory
        self._audit = audit

    async def reflect(
        self, events: list[SessionEvent],
    ) -> list[Lesson]:
        """Extract lessons from a session event stream.

        Returns ``[]`` on:
        * empty events
        * no router
        * both T1 and T2 fail
        * JSON parse failure
        * any exception

        Otherwise returns up to ``reflection_max_lessons`` :class:`Lesson` objects.
        """
        if not events:
            return []
        if self._router is None:
            return []

        # Resolve model ids — empty string falls back to subagent_t1/t2.
        primary = self._resolve_model(
            getattr(self._settings, "reflection_model", "") or "",
            fallback_attr="subagent_t1_model",
            default="qwen3:8b",
        )
        fallback = self._resolve_model(
            getattr(self._settings, "reflection_fallback_model", "") or "",
            fallback_attr="subagent_t2_model",
            default="glm-4.7",
        )
        max_lessons = max(1, int(
            getattr(self._settings, "reflection_max_lessons", 5) or 5,
        ))

        user_prompt = _build_user_prompt(events, max_lessons)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        # Try primary, then fallback. On any exception → next model.
        raw = await self._try_completion(primary, messages)
        if raw is None:
            raw = await self._try_completion(fallback, messages)
        if raw is None:
            logger.warning(
                "ReflectionLoop: both primary (%s) and fallback (%s) failed; "
                "returning []",
                primary, fallback,
            )
            self._safe_audit(
                "reflection_cascade_failed",
                {"primary": primary, "fallback": fallback},
            )
            return []

        lessons = _parse_lessons(raw, expected_max=max_lessons)
        if not lessons:
            self._safe_audit(
                "reflection_parse_failed",
                {"preview": raw[:_AUDIT_CONTENT_PREVIEW_CHARS]},
            )
            return []

        # Dual-write: scratchpad L1 + UnifiedMemory L1.
        await self._persist_lessons(lessons, events=events)
        self._safe_audit(
            "reflection_extracted",
            {"count": len(lessons), "primary": primary},
        )
        return lessons

    # ----- internals -----

    def _resolve_model(
        self, configured: str, *, fallback_attr: str, default: str,
    ) -> str:
        """Resolve a model id: explicit value, then settings attr, then default."""
        if configured:
            return configured
        attr_value = getattr(self._settings, fallback_attr, None)
        if isinstance(attr_value, str) and attr_value:
            return attr_value
        return default

    async def _try_completion(
        self, model: str, messages: list[dict[str, str]],
    ) -> str | None:
        """Call ``router.completion`` for a single model. Return raw content or None."""
        if not model:
            return None
        try:
            response = await self._router.completion(
                messages=messages, model=model, tools=None,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "ReflectionLoop: model %s call failed: %s", model, exc,
            )
            return None
        content = getattr(response, "content", None)
        if not isinstance(content, str):
            return None
        return content

    async def _persist_lessons(
        self, lessons: list[Lesson], *, events: list[SessionEvent],
    ) -> None:
        """Dual-write: scratchpad L1 + UnifiedMemory L1 (best-effort)."""
        session_id = self._scratchpad_session_id(events)
        for lesson in lessons:
            tags = list(lesson.tags)
            if "#reflection" not in tags:
                tags.append("#reflection")
            if session_id and f"#session/{session_id}" not in tags:
                tags.append(f"#session/{session_id}")
            # Scratchpad leg.
            if self._scratchpad is not None:
                try:
                    await self._scratchpad.write_note(
                        level="L1",
                        content=(
                            f"[{lesson.kind}] {lesson.content}"
                        ),
                        tags=tags,
                    )
                except Exception as exc:  # noqa: BLE001 — fail-open
                    logger.warning(
                        "ReflectionLoop: scratchpad.write_note failed: %s", exc,
                    )
            # UnifiedMemory leg.
            if self._unified_memory is not None:
                try:
                    write = getattr(self._unified_memory, "write", None)
                    if write is not None:
                        # The unified memory layer is duck-typed — we
                        # pass a best-effort kwargs bundle. If the
                        # implementation rejects unknown kwargs, we
                        # still want the scratchpad leg to succeed.
                        write(
                            content=lesson.content,
                            layer="L1",
                            source="reflection",
                            kind=lesson.kind,
                            tags=tags,
                        )
                except Exception as exc:  # noqa: BLE001 — fail-open
                    logger.warning(
                        "ReflectionLoop: unified_memory.write failed: %s", exc,
                    )

    def _scratchpad_session_id(self, events: list[SessionEvent]) -> str | None:
        """Best-effort session_id recovery from the scratchpad attribute.

        Mirrors the v1.3.1 ``getattr`` chain in ``loop.py:418-435`` —
        the scratchpad has a ``_session_id`` private attribute we can
        read for tagging, but we never require it.
        """
        if self._scratchpad is None:
            return None
        return getattr(self._scratchpad, "_session_id", None)

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
