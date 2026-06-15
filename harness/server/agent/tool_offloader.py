"""Phase 3 v1.3.1: tool result offload (>25k tokens → L2 scratchpad).

The :class:`ToolOffloader` is the "offload to file" strategy from the
Anthropic context-engineering playbook, applied specifically to
``tool`` messages in the agent loop. When a single tool result exceeds
``settings.tool_offload_threshold_bytes`` (default 25 KB) the full
content is persisted to L2 scratchpad as a regular note (tagged
``#tool-offload``) and the in-flight ``tool`` message is replaced with
a small stub that points at the note id and includes a 3-line preview
of the original output.

This keeps the message history small (chat loop never carries >25 KB
per tool message) while preserving full recoverability — the LLM can
issue a follow-up ``scratchpad_read_offloaded(id=N)`` tool call to
pull the entire body, or ``scratchpad_search_offloaded(query)`` to
re-find an offloaded note semantically (via the v1.3.0
:class:`~harness.agents.l2_retriever.L2Retriever`).

**Fail-open:** any failure in the offload path (scratchpad missing,
write raises, store timeout) returns ``None`` and the loop keeps the
full content. The LLM is never exposed to an offload error.

**Trust boundary:** ``runner.py`` does NOT import this module. The
:class:`~harness.agents.runner.AgentRunner` accepts an
``offloader_factory`` callable (mirror of ``scratchpad_factory``) and
the factory closure lives in ``server/app.py`` lifespan. Verified by
``test_runner_does_not_import_tool_offloader``.

**Storage path:** reuse the existing ``agent-jobs.db`` (sibling of
``scratchpad_notes`` / ``compact_store`` / ``merge_jobs``). L2 is
unbounded by design (the L0 cap does not apply to ``#tool-offload``
notes — offloaded content can be tens or hundreds of KB per note).
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from harness.config import Settings
from harness.agents.scratchpad import NoteLevel

if TYPE_CHECKING:
    from harness.agents.scratchpad_store import ScratchpadStore
    from harness.context.scratchpad_audit import ScratchpadAudit

logger = logging.getLogger(__name__)


# Offload tags — exposed as constants so tests can assert against
# the same values without re-deriving the format.
TOOL_OFFLOAD_TAG = "#tool-offload"
TOOL_TAG_PREFIX = "#tool/"


# Strip control characters (0x00-0x1F) except for newline and tab
# so the stub preview doesn't carry binary garbage. Whitespace other
# than \n/\t is preserved.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class ToolOffloader:
    """Offload large tool results to L2 scratchpad (Phase 3 v1.3.1).

    Parameters
    ----------
    scratchpad:
        The per-``(session_id, agent_id)`` :class:`ScratchpadStore`
        bound to the running agent. Used to write/read the offloaded
        L2 notes.
    settings:
        The harness :class:`Settings` instance. Read once at
        construction time (offload threshold, preview limits, read
        chunk size, per-call timeout) so the offload path is hot-path
        cheap.
    audit:
        Optional :class:`ScratchpadAudit` writer. When provided AND
        ``settings.scratchpad_audit_log`` is True, every successful
        offload emits a ``"tool_offload"`` event with ``note_id``,
        ``tool_name``, ``original_bytes``, ``tool_call_id``.
    """

    def __init__(
        self,
        scratchpad: "ScratchpadStore",
        settings: Settings,
        *,
        audit: "ScratchpadAudit | None" = None,
    ) -> None:
        self._scratchpad = scratchpad
        self._settings = settings
        self._audit = audit

    # --- public API ---

    def should_offload(self, content: str) -> bool:
        """Return True if ``content`` exceeds the offload threshold.

        Uses UTF-8 byte length when the string is encodable; falls back
        to character count when the string contains unpaired
        surrogates (mirrors the fail-open approach in
        :mod:`harness.redaction`).
        """
        if not self._settings.tool_offload_enabled:
            return False
        if not isinstance(content, str) or not content:
            return False
        try:
            byte_size = len(content.encode("utf-8"))
        except (UnicodeEncodeError, ValueError):
            byte_size = len(content)
        return byte_size > self._settings.tool_offload_threshold_bytes

    async def offload(
        self,
        content: str,
        *,
        tool_name: str,
        session_id: str,
        tool_call_id: str | None = None,
    ) -> int | None:
        """Persist ``content`` to L2 if above threshold. Returns note id.

        Returns ``None`` when:

          * ``should_offload()`` is False (caller keeps the full text),
          * the write raises (fail-open, caller keeps the full text),
          * ``content`` is empty or not a ``str``.

        The caller is expected to use the returned id to build a stub
        via :meth:`build_stub`.
        """
        if not self.should_offload(content):
            return None
        if not isinstance(content, str) or not content:
            return None
        # Build tags. We DO NOT add the per-session tag here — the
        # store injects ``#session/{session_id}`` automatically via
        # its own filter. We only add tool-offload and per-tool tags
        # so the read tool can filter cheaply.
        tags = [TOOL_OFFLOAD_TAG, f"{TOOL_TAG_PREFIX}{tool_name}"]
        try:
            note = await self._scratchpad.write_note(
                NoteLevel.L2,
                content,
                tags=tags,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "tool offload failed for tool=%s session=%s: %s",
                tool_name, session_id, exc,
            )
            return None
        # Audit (best-effort, never raises). The audit config is
        # re-read on every call so the operator can flip the setting
        # without rewiring the runtime.
        if self._audit is not None and getattr(
            self._settings, "scratchpad_audit_log", False,
        ):
            try:
                original_bytes = len(content.encode("utf-8"))
            except (UnicodeEncodeError, ValueError):
                original_bytes = len(content)
            self._audit.record(
                event="tool_offload",
                session_id=session_id,
                note_id=note.id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                original_bytes=original_bytes,
                tags_count=len(tags),
            )
        logger.info(
            "tool_offload tool=%s session=%s note_id=%d bytes=%d",
            tool_name, session_id, note.id,
            len(content.encode("utf-8", errors="replace")),
        )
        return note.id

    async def read(
        self,
        note_id: int,
        *,
        max_bytes: int | None = None,
    ) -> str | None:
        """Read an offloaded note by id, truncated to ``max_bytes``.

        Returns ``None`` on miss (no row, or row not tagged with
        ``#tool-offload``) or on store error. The caller is expected
        to surface a graceful error to the LLM.
        """
        if not isinstance(note_id, int) or note_id <= 0:
            return None
        if max_bytes is None:
            max_bytes = self._settings.tool_offload_read_max_bytes
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            max_bytes = 4096
        try:
            # Phase 3 v1.3.0: read_notes accepts a level filter.
            # We pull all L2 notes and find the matching id — the
            # store does not yet expose a get_by_id primitive and we
            # don't want to add one for a single caller.
            notes = await self._scratchpad.read_notes(NoteLevel.L2, limit=500)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("tool offload read failed for id=%d: %s", note_id, exc)
            return None
        for n in notes:
            if n.id == note_id and TOOL_OFFLOAD_TAG in n.tags:
                return n.content[:max_bytes]
        return None

    def build_stub(
        self,
        content: str,
        *,
        note_id: int,
        tool_name: str,
    ) -> str:
        """Build the stub that replaces the full tool result in the
        message history.

        The stub is plain text (the LLM sees it via the ``content`` of
        the ``tool`` role message). It contains:

          1. A one-line header with the byte count, the offloaded note
             id, and the tool name.
          2. A preview: first ``settings.tool_offload_preview_lines``
             non-empty lines of the original content, capped at
             ``settings.tool_offload_preview_max_chars`` characters.
          3. A read-hint footer pointing the LLM at
             ``scratchpad_read_offloaded(id=N)`` and
             ``scratchpad_search_offloaded(query)``.

        Control characters other than ``\\n`` and ``\\t`` are stripped
        from the preview so the stub stays readable on binary output
        (e.g. ``grep -a`` over a binary file). Unpaired surrogates in
        the original content are tolerated by the strip step
        (regex operates on the str directly).
        """
        try:
            byte_size = len(content.encode("utf-8"))
        except (UnicodeEncodeError, ValueError):
            byte_size = len(content)
        preview = self._preview_block(content)
        return (
            f"[Tool result offloaded: {byte_size} bytes, id={note_id}, "
            f"tool={tool_name}]\n\n"
            f"{preview}\n\n"
            f"Read full result via "
            f"scratchpad_read_offloaded(id={note_id}). "
            f"Search across offloaded content via "
            f"scratchpad_search_offloaded(query)."
        )

    # --- internals ---

    def _preview_block(self, content: str) -> str:
        """Return the first N lines (max M chars) of ``content``.

        Lines are split on ``\\n``; empty lines are skipped so the
        preview starts with the first meaningful row. The result is
        stripped of control characters other than ``\\n`` and ``\\t``
        and capped at ``tool_offload_preview_max_chars`` characters.
        """
        max_lines = max(1, self._settings.tool_offload_preview_lines)
        max_chars = max(64, self._settings.tool_offload_preview_max_chars)
        lines: list[str] = []
        for raw_line in content.splitlines():
            stripped = _CONTROL_CHARS_RE.sub("", raw_line)
            if not stripped.strip():
                continue
            lines.append(stripped)
            if len(lines) >= max_lines:
                break
        preview = "\n".join(lines)
        if len(preview) > max_chars:
            preview = preview[: max_chars - 1] + "…"
        return preview


__all__ = ["ToolOffloader", "TOOL_OFFLOAD_TAG", "TOOL_TAG_PREFIX"]
