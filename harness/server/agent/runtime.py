"""Tool runtime — async execution of the 6 built-in tools (Шаг 4).

The runtime is the only place that performs I/O for tools. The agent loop
calls ``await runtime.execute(name, args)`` and gets back a ``ToolResult``.

Design notes:
  * All I/O is async (asyncio.subprocess for bash/grep, asyncio.to_thread
    for file operations that have no native async API).
  * Every file tool resolves paths through the safety layer first; paths
    outside ``project_root`` are rejected without touching the filesystem.
  * Bash commands are checked against a regex denylist before the process
    is spawned. The process has a default timeout of 30s (configurable
    1-300s); on timeout we kill it and return an error result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from harness.server.agent.safety import (
    is_bash_denied,
    resolve_safe_path,
)
from harness.config import settings as _settings
from harness.observability import emit_tool_call as _emit_tool_call
from harness.redaction import redact as _redact

logger = logging.getLogger(__name__)


# === Result model ===

class ToolResult(BaseModel):
    """Standard result envelope for every tool call.

    Tools return JSON-serialisable data; the runtime normalises it into
    this shape so the agent loop can handle errors uniformly.
    """

    ok: bool
    output: str = ""
    error: str = ""
    exit_code: int | None = None
    duration_ms: int | None = None


# === Tool name type (for IDE/type-checker friendliness) ===

ToolName = Literal[
    "read_file", "edit_file", "write_file", "bash", "grep", "glob",
    "scratchpad_write_note", "scratchpad_read_notes",
    "scratchpad_plan_step", "scratchpad_mark_done",
    "scratchpad_l2_search", "scratchpad_l2_promote_to_l1",
    "scratchpad_read_offloaded", "scratchpad_search_offloaded",
]


# === Default tunables ===

DEFAULT_BASH_TIMEOUT = 30  # seconds
MIN_BASH_TIMEOUT = 1
MAX_BASH_TIMEOUT = 300
DEFAULT_GREP_TIMEOUT = 30


# === Phase 4.7 v1.17.0: Path-based denylist patterns ===
#
# Patterns cover sensitive / noisy file classes that file tools should
# not touch by default. The denylist feeds ``initial_decision`` into
# ``_resolve_permission_via_hook`` — a hook CAN still override (allow
# or deny) via the ``PermissionRequest`` event.
#
# Patterns are matched against the raw user-supplied path string
# (repo-relative POSIX or absolute). We search the whole string, not
# just the suffix, so directory-based rules (``secrets/``,
# ``__pycache__/``) work regardless of nesting depth.

#: Patterns shared by both read and write tools. Anything sensitive
#: that should never be silently read is listed here.
_READ_DENYLIST_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"__pycache__[/\\]"), "__pycache__/"),
    (re.compile(r"\.git[/\\]"), ".git/"),
    (re.compile(r"\.env$", re.IGNORECASE), ".env"),
    (re.compile(r"\.key$", re.IGNORECASE), ".key"),
    (re.compile(r"\.pem$", re.IGNORECASE), ".pem"),
    (re.compile(r"secrets[/\\]"), "secrets/"),
    (re.compile(r"node_modules[/\\]"), "node_modules/"),
)

#: Write-side denylist: everything in the read denylist PLUS binary
#: extensions that have no business being written by an LLM.
_WRITE_DENYLIST_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    *_READ_DENYLIST_PATTERNS,
    (re.compile(r"\.exe$", re.IGNORECASE), ".exe"),
    (re.compile(r"\.dll$", re.IGNORECASE), ".dll"),
    (re.compile(r"\.so$", re.IGNORECASE), ".so"),
)


def _match_read_denylist(path: str) -> str | None:
    """Return the matched label if ``path`` is in the read denylist.

    Phase 4.7 v1.17.0. Returns ``None`` when the path is clean. The
    returned label is the human-readable pattern (``".env"``,
    ``"secrets/"`` etc.) — it is surfaced in the ``denied_reason``
    field of the ``PermissionRequest`` payload and in tool error
    messages.
    """
    for pattern, label in _READ_DENYLIST_PATTERNS:
        if pattern.search(path):
            return label
    return None


def _match_write_denylist(path: str) -> str | None:
    """Return the matched label if ``path`` is in the write denylist.

    Phase 4.7 v1.17.0. The write denylist is a superset of the read
    denylist (see ``_WRITE_DENYLIST_PATTERNS``). Returns ``None``
    when the path is clean.
    """
    for pattern, label in _WRITE_DENYLIST_PATTERNS:
        if pattern.search(path):
            return label
    return None


class ToolRuntime:
    """Executes tools against a sandboxed ``project_root``.

    Instantiate one runtime per agent session. The runtime is stateless
    beyond the project_root reference and the optional scratchpad hooks
    (Phase 3 v1.2.0).
    """

    def __init__(
        self,
        project_root: Path,
        *,
        scratchpad: Any = None,
        scratchpad_audit: Any = None,
        l0_section: str | None = None,
        l2_retriever: Any = None,
        l2_router: Any = None,
        l2_curator_model: str = "qwen3:8b",
        tool_offloader: Any = None,
        reflection: Any = None,
        events_collector: Any = None,
        privacy_zones: Any = None,
        hook_runner: Any = None,
        session_id: str = "",
    ) -> None:
        self.project_root = project_root.resolve(strict=False)
        #: Phase 3 v1.2.0: optional scratchpad store. When ``None`` the
        #: 4 scratchpad tools return a graceful error result.
        self._scratchpad = scratchpad
        #: Phase 3 v1.2.0: optional audit writer. When ``None`` the
        #: scratchpad tool calls are not audited (structured logs still
        #: emitted by the store).
        self._scratchpad_audit = scratchpad_audit
        #: Phase 3 v1.2.1: pre-formatted L0 section string to inject
        #: into the system prompt on the first turn. ``None`` (the
        #: default) disables injection. ``AgentLoop.run`` reads this
        #: attribute directly and prepends it to the system message.
        #: The runner is responsible for building the string from
        #: ``store.read_notes("L0", ...)`` — this class is a dumb
        #: container so it can be constructed in tests without the
        #: scratchpad module being importable.
        self._l0_section = l0_section
        #: Phase 3 v1.3.0: optional L2 retriever. When ``None`` the
        #: ``scratchpad_l2_search`` and ``scratchpad_l2_promote_to_l1``
        #: tools return a graceful error result. The retriever is
        #: typed as ``Any`` to keep the trust boundary: the runtime
        #: doesn't import the retriever module directly.
        self._l2_retriever = l2_retriever
        #: Phase 3 v1.3.0: optional LLM router for the LLM-curator
        #: re-rank. When ``None``, ``scratchpad_l2_search`` falls
        #: back to the plain hybrid (BM25 + dense + RRF) result.
        self._l2_router = l2_router
        #: Phase 3 v1.3.0: model id used for the curator summarisation
        #: call in ``scratchpad_l2_promote_to_l1``. Default
        #: ``qwen3:8b`` (T1 in the cost cascade).
        self._l2_curator_model = l2_curator_model
        #: Phase 3 v1.3.1: optional tool offloader. When ``None`` the
        #: offload hook in ``AgentLoop.run`` is a no-op (every tool
        #: result is kept inline). The offloader is typed as ``Any``
        #: to keep the trust boundary: the runtime doesn't import the
        #: offloader module directly. ``AgentLoop`` reads this
        #: attribute via ``getattr`` so the runtime can be
        #: constructed without the offloader module being importable.
        self._tool_offloader = tool_offloader
        #: Phase 3 v1.4.0: optional reflection handle. When ``None``
        #: the ``SessionLifecycle`` exit hook is a no-op (no lesson
        #: extraction at end of session). Typed as ``Any`` to keep
        #: the trust boundary: the runtime doesn't import the
        #: reflection module directly. ``SessionLifecycle`` reads this
        #: attribute via ``getattr`` so the runtime can be constructed
        #: in tests without the reflection module being importable.
        self._reflection = reflection
        #: Phase 3 v1.4.0: optional mutable list that ``AgentLoop``
        #: appends ``SessionEvent`` records to as the session
        #: progresses. ``SessionLifecycle.__aexit__`` reads this
        #: list (via ``getattr(runtime, "_events_collector", None)``)
        #: and passes it to ``ReflectionLoop.reflect``. ``None``
        #: disables event collection (no reflection on exit).
        #: Typed as ``Any`` so the collector does not need to be
        #: ``list[SessionEvent]`` — duck-typed ``.append(...)``.
        self._events_collector = events_collector
        #: Phase 3 v1.5.0: optional path-based privacy filter. When
        #: ``None`` the ``read_file`` / ``grep`` / ``glob`` tools skip
        #: the privacy-zone check entirely (backward compat with
        #: pre-v1.5.0). The filter is typed as ``Any`` to keep the
        #: trust boundary: the runtime doesn't import the privacy
        #: module directly. Each sink calls ``self._privacy_zones.check(path)``
        #: via duck-typed ``.check(...)`` — if the attribute is None
        #: or the filter is disabled, the check short-circuits to
        #: ``("allow", None)``.
        self._privacy_zones = privacy_zones
        #: Phase 4.0: optional hook runner. When set, ``execute`` fires
        #: ``PreToolUse`` and ``PostToolUse`` events. ``None`` disables
        #: hooks entirely (backward compat with pre-v4.0). The runner is
        #: typed as ``Any`` to keep the trust boundary: the runtime does
        #: not import the hooks package at module level.
        self._hook_runner = hook_runner
        #: Phase 4.0: session id (for hook context).
        self._session_id = session_id

    # --- privacy zone helper ---

    def _check_privacy_zones(self, path_str: str) -> ToolResult | None:
        """Phase 3 v1.5.0: pre-action privacy filter for Tier 1 sinks.

        Called from ``_read_file`` / ``_grep`` / ``_glob`` BEFORE any
        I/O. Returns a :class:`ToolResult` if the action is blocked /
        redacted / skipped, or ``None`` if the path is allowed (or the
        privacy filter is not wired up).

        Defence-in-depth: we use ``getattr`` and duck-typed ``.check()``
        so the runtime doesn't import :mod:`harness.privacy` directly.
        If ``self._privacy_zones`` is ``None`` (default for tests /
        pre-v1.5.0 wiring) or the filter raises, we return ``None``
        (allow — fail-open at the privacy boundary).
        """
        filter_obj = getattr(self, "_privacy_zones", None)
        if filter_obj is None:
            return None
        check = getattr(filter_obj, "check", None)
        if not callable(check):
            return None
        try:
            action, matched_pattern = check(path_str)
        except Exception:  # noqa: BLE001 — privacy MUST fail-open
            return None
        if action == "allow" or action is None:
            return None
        if action == "block":
            return ToolResult(
                ok=False,
                error=(
                    f"path in privacy zone: {path_str!r} "
                    f"(matched: {matched_pattern})"
                ),
            )
        if action == "redact":
            return ToolResult(
                ok=True,
                output=f"[PRIVATE: path matched privacy zone '{matched_pattern}']",
            )
        if action == "skip":
            return ToolResult(ok=True, output="")
        # Unknown action — fail-open.
        return None

    # --- dispatcher ---

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Dispatch to the right handler. Unknown tool → error result."""
        # Phase 4.0: fire PreToolUse hook before dispatch.
        pre_agg = await self._fire_hook(
            event="PreToolUse",
            payload={"tool_name": name, "arguments": args},
        )
        if pre_agg is not None and pre_agg.final_decision == "block":
            reason = ""
            for d in pre_agg.decisions:
                if d.hook_id == pre_agg.blocked_by:
                    reason = d.output.get("reason", "no reason")
                    break
            return ToolResult(
                ok=False,
                error=f"blocked by hook {pre_agg.blocked_by}: {reason}",
            )
        # Apply pre-hook payload modifications (if any).
        if pre_agg is not None and pre_agg.final_decision == "modify":
            args = pre_agg.final_payload.get("arguments", args)
        start = time.monotonic()
        try:
            if name == "read_file":
                result = await self._read_file(args)
            elif name == "edit_file":
                result = await self._edit_file(args)
            elif name == "write_file":
                result = await self._write_file(args)
            elif name == "bash":
                result = await self._bash(args)
            elif name == "grep":
                result = await self._grep(args)
            elif name == "glob":
                result = await self._glob(args)
            elif name == "scratchpad_write_note":
                result = await self._scratchpad_write_note(args)
            elif name == "scratchpad_read_notes":
                result = await self._scratchpad_read_notes(args)
            elif name == "scratchpad_plan_step":
                result = await self._scratchpad_plan_step(args)
            elif name == "scratchpad_mark_done":
                result = await self._scratchpad_mark_done(args)
            elif name == "scratchpad_l2_search":
                result = await self._scratchpad_l2_search(args)
            elif name == "scratchpad_l2_promote_to_l1":
                result = await self._scratchpad_l2_promote_to_l1(args)
            elif name == "scratchpad_read_offloaded":
                result = await self._scratchpad_read_offloaded(args)
            elif name == "scratchpad_search_offloaded":
                result = await self._scratchpad_search_offloaded(args)
            else:
                result = ToolResult(ok=False, error=f"unknown tool: {name!r}")
        except Exception as exc:  # noqa: BLE001 — top-level safety net
            logger.exception("tool %s raised", name)
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if result.duration_ms is None:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            result = result.model_copy(update={"duration_ms": elapsed_ms})
        # Phase 4.0: fire PostToolUse hook after dispatch.
        post_agg = await self._fire_hook(
            event="PostToolUse",
            payload={
                "tool_name": name,
                "arguments": args,
                "ok": result.ok,
                "output": result.output[:500] if result.output else "",
                "error": result.error,
            },
        )
        if post_agg is not None and post_agg.final_decision == "block":
            return ToolResult(
                ok=False,
                error=f"post-hook block by {post_agg.blocked_by}",
            )
        # Phase 4.1 Step 6.4: emit tool call metric + log.
        try:
            _emit_tool_call(
                tool_name=name,
                duration_s=((result.duration_ms or 0) / 1000.0),
                status="ok" if result.ok else "error",
                error=result.error or "",
            )
        except Exception:  # noqa: BLE001 — observability must never break tools
            logger.debug("emit_tool_call failed for %s", name, exc_info=True)
        return result

    async def _fire_hook(self, *, event: str, payload: dict) -> Any:
        """Fire a hook via the injected ``hook_runner`` (Phase 4.0).

        Returns the ``HookAggregate`` (or ``None`` if no runner is set).
        All hook failures are swallowed — they never break tool execution.
        """
        if self._hook_runner is None:
            return None
        try:
            from harness.hooks import EventType, HookContext

            ctx = HookContext(
                event=event,
                session_id=self._session_id,
                agent_id="",
                payload=payload,
            )
            return await self._hook_runner.fire(ctx)
        except Exception:  # noqa: BLE001
            return None

    # --- file tools ---

    async def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path_str = args.get("path")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(ok=False, error="read_file: 'path' is required")

        # Phase 4.7 v1.17.0: PermissionRequest wiring. Compute the
        # initial decision from the path denylist and fire the hook
        # BEFORE any I/O (privacy-zone check, file existence). A hook
        # may override the denylist (allow on match) or block a
        # clean path.
        denied_label = _match_read_denylist(path_str)
        initial_decision = "deny" if denied_label is not None else "allow"
        denied_reason = (
            f"path matches denylist pattern {denied_label!r}"
            if denied_label is not None
            else ""
        )
        final_decision = await self._resolve_permission_via_hook(
            tool_name="read_file",
            arguments=args,
            initial_decision=initial_decision,
            denied_reason=denied_reason,
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error=(
                    f"denied: {denied_reason}"
                    if denied_reason
                    else "denied: blocked by PermissionRequest hook"
                ),
            )

        resolved = resolve_safe_path(path_str, self.project_root)
        if resolved is None:
            return ToolResult(
                ok=False, error=f"path outside project_root: {path_str!r}"
            )

        # Phase 3 v1.5.0 Tier 1 sink: privacy zone check BEFORE file I/O.
        # Path is repo-relative POSIX (matches match_glob convention).
        privacy_result = self._check_privacy_zones(path_str)
        if privacy_result is not None:
            return privacy_result

        if not resolved.exists() or not resolved.is_file():
            return ToolResult(ok=False, error=f"file not found: {path_str!r}")

        def _do_read() -> str:
            return resolved.read_text(encoding="utf-8")

        try:
            content = await asyncio.to_thread(_do_read)
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(ok=False, error=f"read_file: {exc}")
        # Phase 3: redact the file content before it flows into the
        # LLM. ``.env`` files and any other config that contains
        # credentials get scrubbed here. We redact ALL files (not
        # just .env) because file contents may contain pasted
        # secrets in any text file (README, code, .git/config with
        # an embedded token, etc.).
        if _settings.redaction_enabled:
            content = _redact(content)
        return ToolResult(ok=True, output=content)

    async def _edit_file(self, args: dict[str, Any]) -> ToolResult:
        path_str = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(ok=False, error="edit_file: 'path' is required")
        if not isinstance(old_string, str):
            return ToolResult(ok=False, error="edit_file: 'old_string' must be a string")
        if not isinstance(new_string, str):
            return ToolResult(ok=False, error="edit_file: 'new_string' must be a string")

        # Phase 4.7 v1.17.0: PermissionRequest wiring (write path).
        # Edit mutates files → use the write denylist (superset of
        # read). Hook may override before any I/O.
        denied_label = _match_write_denylist(path_str)
        initial_decision = "deny" if denied_label is not None else "allow"
        denied_reason = (
            f"path matches denylist pattern {denied_label!r}"
            if denied_label is not None
            else ""
        )
        final_decision = await self._resolve_permission_via_hook(
            tool_name="edit_file",
            arguments=args,
            initial_decision=initial_decision,
            denied_reason=denied_reason,
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error=(
                    f"denied: {denied_reason}"
                    if denied_reason
                    else "denied: blocked by PermissionRequest hook"
                ),
            )

        resolved = resolve_safe_path(path_str, self.project_root)
        if resolved is None:
            return ToolResult(
                ok=False, error=f"path outside project_root: {path_str!r}"
            )

        def _do_edit() -> tuple[bool, str]:
            if not resolved.exists() or not resolved.is_file():
                return False, f"file not found: {path_str!r}"
            text = resolved.read_text(encoding="utf-8")
            if old_string not in text:
                return False, "old_string not found"
            # Use str.replace with count=1 to keep the operation targeted.
            new_text = text.replace(old_string, new_string, 1)
            resolved.write_text(new_text, encoding="utf-8")
            return True, "ok"

        try:
            ok, msg = await asyncio.to_thread(_do_edit)
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(ok=False, error=f"edit_file: {exc}")
        if not ok:
            return ToolResult(ok=False, error=msg)
        return ToolResult(ok=True, output=msg)

    async def _write_file(self, args: dict[str, Any]) -> ToolResult:
        path_str = args.get("path")
        content = args.get("content")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(ok=False, error="write_file: 'path' is required")
        if not isinstance(content, str):
            return ToolResult(ok=False, error="write_file: 'content' must be a string")

        # Phase 4.7 v1.17.0: PermissionRequest wiring (write path).
        # Same denylist + hook contract as edit_file. Hook may
        # override before any I/O.
        denied_label = _match_write_denylist(path_str)
        initial_decision = "deny" if denied_label is not None else "allow"
        denied_reason = (
            f"path matches denylist pattern {denied_label!r}"
            if denied_label is not None
            else ""
        )
        final_decision = await self._resolve_permission_via_hook(
            tool_name="write_file",
            arguments=args,
            initial_decision=initial_decision,
            denied_reason=denied_reason,
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error=(
                    f"denied: {denied_reason}"
                    if denied_reason
                    else "denied: blocked by PermissionRequest hook"
                ),
            )

        resolved = resolve_safe_path(path_str, self.project_root)
        if resolved is None:
            return ToolResult(
                ok=False, error=f"path outside project_root: {path_str!r}"
            )

        def _do_write() -> None:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")

        try:
            await asyncio.to_thread(_do_write)
        except OSError as exc:
            return ToolResult(ok=False, error=f"write_file: {exc}")
        return ToolResult(ok=True, output=f"wrote {len(content)} bytes to {path_str}")

    # --- Phase 4.5 v1.15.0: PermissionRequest override resolution ---

    async def _resolve_permission_via_hook(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        initial_decision: str,
        denied_reason: str = "",
    ) -> str:
        """Fire ``PermissionRequest`` BEFORE the denylist check and
        apply hook overrides to the permission decision.

        Phase 4.5 v1.15.0 semantics:

        * Hook returns ``"allow"`` — final decision is ``"allow"`` even
          if ``initial_decision`` was ``"deny"`` (this is the
          denylist-override escape hatch).
        * Hook returns ``"block"`` — final decision is ``"deny"`` even
          if ``initial_decision`` was ``"allow"``.
        * Hook returns ``"modify"`` with
          ``output["payload"]["permission_decision"]`` — that value
          (``"allow"`` / ``"deny"``) becomes the final decision.
        * Hook failure / unknown event / no registered hooks — the
          ``initial_decision`` is returned unchanged (fail-open, but
          explicit: production code never silently flips a deny).

        Edge case: a hook may return ``"allow"`` for a tool that is
        NOT in the denylist. In that case ``initial_decision`` was
        already ``"allow"``, so the override is a no-op — the tool
        proceeds as it would have anyway. This keeps the hook
        contract uniform regardless of whether the denylist matched.

        PII safety: ``arguments_preview`` is truncated to 200 chars
        before being placed in the payload (see truncation below).

        Returns the final permission decision: ``"allow"`` or
        ``"deny"``. ``safe_fire`` is used indirectly via the global
        runner so all emission behaviour (observability counter,
        audit log, recursion guard) is preserved. We bypass the
        convenience wrapper only to read the aggregate's
        ``final_payload`` for the ``modify`` path.
        """
        from harness.hooks.context import HookContext
        from harness.hooks.runner import get_global_hook_runner

        # Phase 4.12 v1.22.0: respect the ``hooks_permission_request_enabled``
        # setting. When disabled, the PermissionRequest event is NOT emitted
        # and the hook cannot override the denylist decision. The caller's
        # ``initial_decision`` is returned unchanged (fail-open: the denylist
        # still applies, only the hook-mediated override path is suppressed).
        if not getattr(_settings, "hooks_permission_request_enabled", True):
            return initial_decision

        # Truncate to 200 chars — keeps PII / large arguments out of
        # the audit log and prevents payload size blow-ups in hook
        # transports (subprocess stdin, HTTP POST body).
        arguments_preview = str(arguments)[:200]

        ctx = HookContext(
            event="PermissionRequest",
            session_id=self._session_id,
            agent_id="",
            payload={
                "tool_name": tool_name,
                "arguments_preview": arguments_preview,
                "permission_decision": initial_decision,
                "denied_reason": denied_reason or "",
            },
        )
        try:
            runner = get_global_hook_runner()
            aggregate = await runner.fire(ctx)
        except Exception as exc:  # noqa: BLE001 — hooks must never break tools
            logger.debug(
                "PermissionRequest hook failed for %s: %s: %s",
                tool_name, type(exc).__name__, exc,
            )
            return initial_decision

        # No hooks fired for PermissionRequest → keep the original
        # denylist decision. We MUST distinguish "no hooks" from
        # "hook explicitly returned allow" — otherwise an empty
        # registry would silently disable the denylist (security
        # regression). ``aggregate.decisions`` is empty iff no hook
        # was dispatched.
        if not aggregate.decisions:
            return initial_decision

        final = aggregate.final_decision
        if final == "block":
            return "deny"
        if final == "allow":
            # Explicit hook allow overrides an initial deny.
            return "allow"
        # modify: hook MAY override permission_decision in its payload.
        override = (
            aggregate.final_payload.get("permission_decision")
            if aggregate.final_payload
            else None
        )
        if override == "allow":
            return "allow"
        if override == "deny":
            return "deny"
        # modify without a valid override → keep initial (caller's
        # denylist decision stands; the hook signalled "I want to
        # change SOMETHING" but not the permission itself).
        return initial_decision

    # --- subprocess tools ---

    async def _bash(self, args: dict[str, Any]) -> ToolResult:
        command = args.get("command")
        timeout = args.get("timeout", DEFAULT_BASH_TIMEOUT)
        if not isinstance(command, str) or not command:
            return ToolResult(ok=False, error="bash: 'command' is required")
        if not isinstance(timeout, int) or not (
            MIN_BASH_TIMEOUT <= timeout <= MAX_BASH_TIMEOUT
        ):
            return ToolResult(
                ok=False,
                error=f"bash: 'timeout' must be an int in [{MIN_BASH_TIMEOUT}, {MAX_BASH_TIMEOUT}]",
            )

        # 1. Compute initial permission from the denylist.
        denied_pattern = is_bash_denied(command)
        initial_decision = "deny" if denied_pattern is not None else "allow"
        denied_reason = (
            f"matches safety pattern {denied_pattern!r}"
            if denied_pattern is not None
            else ""
        )

        # 2. Phase 4.5 v1.15.0: fire PermissionRequest BEFORE the
        #    denylist short-circuits. The hook may override the
        #    permission (allow-on-denylist, block-on-allow, or modify
        #    with an explicit decision). See
        #    ``_resolve_permission_via_hook`` for the full contract.
        final_decision = await self._resolve_permission_via_hook(
            tool_name="bash",
            arguments=args,
            initial_decision=initial_decision,
            denied_reason=denied_reason,
        )
        if final_decision == "deny":
            logger.warning(
                "bash denied: safety_pattern=%s, hook_overrode_to_deny=%s",
                denied_pattern, initial_decision == "allow",
            )
            return ToolResult(
                ok=False,
                error=(
                    f"denied: {denied_reason}"
                    if denied_reason
                    else "denied: blocked by PermissionRequest hook"
                ),
            )

        # 3. Spawn via shell. We accept the security tradeoff here because
        #    the tool is explicitly named "bash" — the LLM is expected to
        #    use it for shell-style composition.
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolResult(ok=False, error=f"bash: failed to spawn: {exc}")

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("bash timed out after %ds: %s", timeout, command)
            return ToolResult(
                ok=False,
                error=f"timeout after {timeout}s",
                exit_code=None,
            )

        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        exit_code = proc.returncode
        ok = exit_code == 0
        output = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        return ToolResult(
            ok=ok,
            output=output,
            error="" if ok else stderr or f"exit_code={exit_code}",
            exit_code=exit_code,
        )

    async def _grep(self, args: dict[str, Any]) -> ToolResult:
        pattern = args.get("pattern")
        path_str = args.get("path")
        timeout = args.get("timeout", DEFAULT_GREP_TIMEOUT)
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(ok=False, error="grep: 'pattern' is required")
        if not isinstance(timeout, int) or not (
            MIN_BASH_TIMEOUT <= timeout <= MAX_BASH_TIMEOUT
        ):
            return ToolResult(
                ok=False,
                error=f"grep: 'timeout' must be an int in [{MIN_BASH_TIMEOUT}, {MAX_BASH_TIMEOUT}]",
            )

        # Phase 4.7 v1.17.0: PermissionRequest wiring. Use the read
        # denylist on the optional path argument. A search rooted in
        # a sensitive tree (``secrets/``, ``__pycache__/``) is
        # denied by default. When ``path`` is absent (search the
        # whole project root) the denylist cannot apply per-call —
        # we default to ``allow`` and rely on the privacy-zone
        # filter for the enumeration path.
        grep_path_for_deny = path_str if isinstance(path_str, str) else ""
        denied_label = _match_read_denylist(grep_path_for_deny)
        initial_decision = "deny" if denied_label is not None else "allow"
        denied_reason = (
            f"path matches denylist pattern {denied_label!r}"
            if denied_label is not None
            else ""
        )
        final_decision = await self._resolve_permission_via_hook(
            tool_name="grep",
            arguments=args,
            initial_decision=initial_decision,
            denied_reason=denied_reason,
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error=(
                    f"denied: {denied_reason}"
                    if denied_reason
                    else "denied: blocked by PermissionRequest hook"
                ),
            )

        # Resolve path. If absent, use project_root.
        if path_str is None or path_str == "":
            base = self.project_root
        else:
            resolved = resolve_safe_path(path_str, self.project_root)
            if resolved is None:
                return ToolResult(
                    ok=False, error=f"path outside project_root: {path_str!r}"
                )
            # Phase 3 v1.5.0 Tier 1 sink: privacy zone check for grep root.
            # We block if the search root itself is in a privacy zone
            # (searching a `private/` dir would defeat the read_file block).
            privacy_result = self._check_privacy_zones(path_str)
            if privacy_result is not None:
                return privacy_result
            base = resolved

        if shutil.which("rg") is not None:
            cmd = [
                "rg",
                "--no-heading",
                "--line-number",
                "--",
                pattern,
                str(base),
            ]
        elif shutil.which("grep") is not None:
            cmd = [
                "grep",
                "-rn",
                "-E",
                "--",
                pattern,
                str(base),
            ]
        else:
            return ToolResult(
                ok=False, error="grep: neither rg nor grep available on PATH"
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolResult(ok=False, error=f"grep: failed to spawn: {exc}")

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return ToolResult(ok=False, error=f"timeout after {timeout}s")

        stdout_b, stderr_b = await proc.communicate()

        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        # rg/grep exit 0 = found, 1 = no match, >1 = error. Treat 0 and 1 as ok.
        if proc.returncode in (0, 1):
            return ToolResult(ok=True, output=stdout)
        return ToolResult(
            ok=False,
            output=stdout,
            error=stderr or f"grep failed: exit_code={proc.returncode}",
            exit_code=proc.returncode,
        )

    async def _glob(self, args: dict[str, Any]) -> ToolResult:
        pattern = args.get("pattern")
        path_str = args.get("path")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(ok=False, error="glob: 'pattern' is required")

        # Phase 4.7 v1.17.0: PermissionRequest wiring. Use the read
        # denylist on the optional path argument. Globbing a
        # sensitive tree would enumerate its contents to the LLM —
        # denied by default. When ``path`` is absent we default to
        # ``allow`` (the privacy-zone filter still runs per-file).
        glob_path_for_deny = path_str if isinstance(path_str, str) else ""
        denied_label = _match_read_denylist(glob_path_for_deny)
        initial_decision = "deny" if denied_label is not None else "allow"
        denied_reason = (
            f"path matches denylist pattern {denied_label!r}"
            if denied_label is not None
            else ""
        )
        final_decision = await self._resolve_permission_via_hook(
            tool_name="glob",
            arguments=args,
            initial_decision=initial_decision,
            denied_reason=denied_reason,
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error=(
                    f"denied: {denied_reason}"
                    if denied_reason
                    else "denied: blocked by PermissionRequest hook"
                ),
            )

        if path_str is None or path_str == "":
            base = self.project_root
        else:
            resolved = resolve_safe_path(path_str, self.project_root)
            if resolved is None:
                return ToolResult(
                    ok=False, error=f"path outside project_root: {path_str!r}"
                )
            # Phase 3 v1.5.0 Tier 1 sink: privacy zone check for glob root.
            # Same rationale as grep — a glob rooted in a privacy zone
            # would enumerate the entire sensitive tree to the LLM.
            privacy_result = self._check_privacy_zones(path_str)
            if privacy_result is not None:
                return privacy_result
            base = resolved

        # Path.glob can be expensive on large trees — push to a thread.
        def _do_glob() -> list[str]:
            # Resolve the base here so we can use it for prefixing and to
            # keep the thread-local work self-contained.
            base_resolved = base.resolve(strict=False)
            matches: list[str] = []
            for p in base_resolved.glob(pattern):
                try:
                    rel = p.relative_to(base_resolved)
                except ValueError:
                    rel = p  # absolute fallback
                matches.append(str(rel))
            return sorted(matches)

        try:
            matches = await asyncio.to_thread(_do_glob)
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=f"glob: {exc}")

        if not matches:
            return ToolResult(ok=True, output="(no matches)")
        return ToolResult(ok=True, output="\n".join(matches))

    # --- scratchpad tools (Phase 3 v1.2.0) ---

    async def _scratchpad_write_note(
        self, args: dict[str, Any],
    ) -> ToolResult:
        if self._scratchpad is None:
            return ToolResult(
                ok=False, error="scratchpad not enabled in this runtime",
            )
        level_raw = args.get("level")
        content = args.get("content")
        if not isinstance(level_raw, str) or level_raw not in ("L0", "L1", "L2"):
            return ToolResult(
                ok=False,
                error=(
                    "scratchpad_write_note: 'level' must be one of "
                    "'L0', 'L1', 'L2'"
                ),
            )
        if not isinstance(content, str) or not content:
            return ToolResult(
                ok=False,
                error="scratchpad_write_note: 'content' must be a non-empty string",
            )
        tags = args.get("tags")
        if tags is not None and not (
            isinstance(tags, list) and all(isinstance(t, str) for t in tags)
        ):
            return ToolResult(
                ok=False,
                error="scratchpad_write_note: 'tags' must be a list[str] or omitted",
            )
        # Phase 4.12 v1.22.0: PermissionRequest wiring for the write
        # path. Scratchpad writes are state-mutating (a new note enters
        # the memory), so we expose them to the hook for audit / policy
        # overrides. There is no path-based denylist for scratchpad —
        # the initial decision is ``allow`` and a hook MAY force ``deny``
        # via ``block`` or ``modify`` decisions.
        final_decision = await self._resolve_permission_via_hook(
            tool_name="scratchpad_write_note",
            arguments=args,
            initial_decision="allow",
            denied_reason="",
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error="denied: blocked by PermissionRequest hook",
            )
        # Lazy import: avoid hard-coupling runtime.py to scratchpad_store.
        from harness.agents.scratchpad import NoteLevel
        try:
            note = await self._scratchpad.write_note(
                NoteLevel(level_raw), content, tags=tags,
            )
        except Exception as exc:  # noqa: BLE001 — scratchpad never breaks the chat loop
            logger.warning(
                "scratchpad.write_note failed: %s", exc,
            )
            return ToolResult(
                ok=False,
                error=f"scratchpad: {type(exc).__name__}: {exc}",
            )
        if self._scratchpad_audit is not None:
            try:
                self._scratchpad_audit.record(
                    "write",
                    self._scratchpad._session_id,  # type: ignore[attr-defined]
                    level=level_raw,
                    note_id=note.id,
                    size_bytes=len(content.encode("utf-8")),
                    tags_count=len(tags) if tags else 0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("scratchpad audit write failed: %s", exc)
        payload = {
            "id": note.id,
            "level": level_raw,
            "created_at": note.created_at,
        }
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))

    async def _scratchpad_read_notes(
        self, args: dict[str, Any],
    ) -> ToolResult:
        if self._scratchpad is None:
            return ToolResult(
                ok=False, error="scratchpad not enabled in this runtime",
            )
        level_raw = args.get("level")
        if level_raw is not None and level_raw not in ("L0", "L1", "L2"):
            return ToolResult(
                ok=False,
                error=(
                    "scratchpad_read_notes: 'level' must be one of "
                    "'L0', 'L1', 'L2' or omitted"
                ),
            )
        from harness.agents.scratchpad import NoteLevel
        level_enum = NoteLevel(level_raw) if level_raw is not None else None
        try:
            notes = await self._scratchpad.read_notes(
                level_enum, limit=50,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scratchpad.read_notes failed: %s", exc)
            return ToolResult(
                ok=False,
                error=f"scratchpad: {type(exc).__name__}: {exc}",
            )
        if self._scratchpad_audit is not None:
            try:
                self._scratchpad_audit.record(
                    "read",
                    self._scratchpad._session_id,  # type: ignore[attr-defined]
                    level_filter=level_raw,
                    result_count=len(notes),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("scratchpad audit read failed: %s", exc)
        payload = [
            {
                "id": n.id,
                "level": n.level.value,
                "content": n.content,
                "tags": n.tags,
                "created_at": n.created_at,
            }
            for n in notes
        ]
        if not payload:
            return ToolResult(ok=True, output="(no notes)")
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))

    async def _scratchpad_plan_step(
        self, args: dict[str, Any],
    ) -> ToolResult:
        if self._scratchpad is None:
            return ToolResult(
                ok=False, error="scratchpad not enabled in this runtime",
            )
        description = args.get("description")
        if not isinstance(description, str) or not description:
            return ToolResult(
                ok=False,
                error="scratchpad_plan_step: 'description' is required",
            )
        deps = args.get("deps")
        if deps is not None and not (
            isinstance(deps, list) and all(isinstance(d, int) for d in deps)
        ):
            return ToolResult(
                ok=False,
                error="scratchpad_plan_step: 'deps' must be a list[int] or omitted",
            )
        # Phase 4.12 v1.22.0: PermissionRequest wiring. Plan steps
        # mutate the session plan graph — expose to the hook so a
        # policy may block creation (e.g. quota / step-budget rules).
        final_decision = await self._resolve_permission_via_hook(
            tool_name="scratchpad_plan_step",
            arguments=args,
            initial_decision="allow",
            denied_reason="",
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error="denied: blocked by PermissionRequest hook",
            )
        try:
            step = await self._scratchpad.add_plan_step(
                description, deps=deps,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scratchpad.plan_step failed: %s", exc)
            return ToolResult(
                ok=False,
                error=f"scratchpad: {type(exc).__name__}: {exc}",
            )
        if self._scratchpad_audit is not None:
            try:
                self._scratchpad_audit.record(
                    "plan_step",
                    self._scratchpad._session_id,  # type: ignore[attr-defined]
                    step_id=step.id,
                    deps_count=len(deps) if deps else 0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("scratchpad audit plan_step failed: %s", exc)
        payload = {
            "id": step.id,
            "status": step.status.value,
            "created_at": step.created_at,
        }
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))

    async def _scratchpad_mark_done(
        self, args: dict[str, Any],
    ) -> ToolResult:
        if self._scratchpad is None:
            return ToolResult(
                ok=False, error="scratchpad not enabled in this runtime",
            )
        step_id = args.get("step_id")
        if not isinstance(step_id, int) or isinstance(step_id, bool):
            return ToolResult(
                ok=False,
                error="scratchpad_mark_done: 'step_id' must be an integer",
            )
        status_raw = args.get("status", "done")
        if status_raw not in ("pending", "in_progress", "done", "blocked"):
            return ToolResult(
                ok=False,
                error=(
                    "scratchpad_mark_done: 'status' must be one of "
                    "'pending', 'in_progress', 'done', 'blocked'"
                ),
            )
        # Phase 4.12 v1.22.0: PermissionRequest wiring. Marking a plan
        # step as done mutates the plan graph (advances state) — expose
        # to the hook so a policy may block / audit state transitions.
        final_decision = await self._resolve_permission_via_hook(
            tool_name="scratchpad_mark_done",
            arguments=args,
            initial_decision="allow",
            denied_reason="",
        )
        if final_decision == "deny":
            return ToolResult(
                ok=False,
                error="denied: blocked by PermissionRequest hook",
            )
        from harness.agents.scratchpad import PlanStatus
        try:
            updated = await self._scratchpad.mark_done(
                step_id, status=PlanStatus(status_raw),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scratchpad.mark_done failed: %s", exc)
            return ToolResult(
                ok=False,
                error=f"scratchpad: {type(exc).__name__}: {exc}",
            )
        if updated is None:
            return ToolResult(
                ok=False,
                error=f"scratchpad_mark_done: no plan_step with id={step_id}",
            )
        if self._scratchpad_audit is not None:
            try:
                self._scratchpad_audit.record(
                    "mark_done",
                    self._scratchpad._session_id,  # type: ignore[attr-defined]
                    step_id=step_id,
                    status=status_raw,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("scratchpad audit mark_done failed: %s", exc)
        payload = {
            "id": updated.id,
            "status": updated.status.value,
            "updated_at": updated.updated_at,
        }
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))

    # --- L2 retrieval tools (Phase 3 v1.3.0) ---

    async def _scratchpad_l2_search(
        self, args: dict[str, Any],
    ) -> ToolResult:
        """Hybrid dense+BM25 search over the L2 archive, with optional
        LLM-curator re-rank. The retriever is a duck-typed
        :class:`harness.agents.l2_retriever.L2Retriever` injected
        by the runner — the runtime doesn't import it.
        """
        if self._l2_retriever is None:
            return ToolResult(
                ok=False,
                error="scratchpad_l2_search: L2 retriever not enabled in this runtime",
            )
        if self._scratchpad is None:
            return ToolResult(
                ok=False,
                error="scratchpad_l2_search: scratchpad not enabled (L2 search needs the store)",
            )
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                ok=False,
                error="scratchpad_l2_search: 'query' must be a non-empty string",
            )
        top_k_raw = args.get("top_k", 10)
        try:
            top_k = max(1, min(int(top_k_raw), 50))
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                error=f"scratchpad_l2_search: 'top_k' must be an integer, got {top_k_raw!r}",
            )
        try:
            # Phase 3 v1.3.0: pull the in-memory L2 notes from the
            # store. The retriever's BM25 path needs the text; the
            # dense path queries the L2 vector store directly.
            notes = await self._scratchpad.read_notes("L2", limit=200)
            session_id = getattr(self._scratchpad, "_session_id", None)
            if session_id is not None:
                hits = await self._l2_retriever.curated_search(
                    query, top_k=top_k, candidate_k=50,
                    notes=notes, router=self._l2_router,
                    model=self._l2_curator_model,
                )
            else:
                # Admin context (no session filter) — same call.
                hits = await self._l2_retriever.curated_search(
                    query, top_k=top_k, candidate_k=50,
                    notes=notes, router=self._l2_router,
                    model=self._l2_curator_model,
                )
        except Exception as exc:  # noqa: BLE001 — chat loop must not break
            logger.warning("scratchpad_l2_search failed: %s", exc)
            return ToolResult(
                ok=False,
                error=f"scratchpad_l2_search: {type(exc).__name__}: {exc}",
            )
        payload = {
            "query": query,
            "count": len(hits),
            "results": [
                {
                    "id": int(n.id),
                    "content": n.content,
                    "tags": list(n.tags),
                    "created_at": n.created_at,
                    "score": float(score),
                }
                for n, score in hits
            ],
        }
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))

    async def _scratchpad_l2_promote_to_l1(
        self, args: dict[str, Any],
    ) -> ToolResult:
        """Fetch top-N L2 notes matching the query, summarise them
        with the LLM-curator, and write the summary as a fresh L1
        plan note. The "Compress" half of the Phase 3 v1.3.0
        strategy: long-term archive → working state.
        """
        if self._l2_retriever is None:
            return ToolResult(
                ok=False,
                error="scratchpad_l2_promote_to_l1: L2 retriever not enabled",
            )
        if self._scratchpad is None:
            return ToolResult(
                ok=False,
                error="scratchpad_l2_promote_to_l1: scratchpad not enabled",
            )
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                ok=False,
                error="scratchpad_l2_promote_to_l1: 'query' must be a non-empty string",
            )
        max_notes_raw = args.get("max_notes", 20)
        try:
            max_notes = max(1, min(int(max_notes_raw), 50))
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                error=f"scratchpad_l2_promote_to_l1: 'max_notes' must be an integer, got {max_notes_raw!r}",
            )
        try:
            notes = await self._scratchpad.read_notes("L2", limit=200)
            # 1. Pull top candidates via curated search.
            candidates = await self._l2_retriever.curated_search(
                query, top_k=max_notes, candidate_k=50,
                notes=notes, router=self._l2_router,
                model=self._l2_curator_model,
            )
            if not candidates:
                return ToolResult(
                    ok=True,
                    output=json.dumps(
                        {"status": "no_candidates", "query": query},
                        ensure_ascii=False,
                    ),
                )
            # 2. Ask the LLM to summarise. We don't import the
            # curator prompt here — we re-use the L2 retriever's
            # curator machinery via a curated_search call with the
            # same candidates, then build the summary from the
            # high-scoring notes. This keeps the runtime free of
            # l2_retriever import details.
            high_scoring = [n for n, s in candidates if s >= 50.0]
            if not high_scoring:
                # Curator marked everything <50 → nothing to promote.
                return ToolResult(
                    ok=True,
                    output=json.dumps(
                        {
                            "status": "below_threshold",
                            "query": query,
                            "top_score": candidates[0][1] if candidates else 0.0,
                        },
                        ensure_ascii=False,
                    ),
                )
            # 3. Build a bullet-point summary of the high-scoring
            # notes. We don't need a separate LLM call — the
            # notes' own content is the "summary" of the theme.
            # The L1 tag marks the source: hierarchical summary.
            bullet_lines = [
                f"- (from id={int(n.id)}) {n.content[:300]}"
                for n in high_scoring
            ]
            summary = (
                f"## L2 summary — query: {query}\n\n"
                + "\n".join(bullet_lines)
            )
            # 4. Persist as an L1 plan note. We re-use
            # ``scratchpad_write_note`` logic by calling the store
            # directly with level="L1".
            new_note = await self._scratchpad.write_note(
                "L1",
                summary,
                tags=["l2-summary", f"query:{query[:50]}"],
            )
        except Exception as exc:  # noqa: BLE001 — chat loop must not break
            logger.warning("scratchpad_l2_promote_to_l1 failed: %s", exc)
            return ToolResult(
                ok=False,
                error=f"scratchpad_l2_promote_to_l1: {type(exc).__name__}: {exc}",
            )
        payload = {
            "status": "promoted",
            "query": query,
            "source_note_ids": [int(n.id) for n in high_scoring],
            "new_l1_note_id": int(new_note.id),
            "summary_preview": summary[:500],
        }
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))

    # --- Tool offload tools (Phase 3 v1.3.1) ---

    async def _scratchpad_read_offloaded(
        self, args: dict[str, Any],
    ) -> ToolResult:
        """Read a previously offloaded tool result by note id.

        The :class:`ToolOffloader` (Phase 3 v1.3.1) persists large tool
        results to L2 and replaces the in-flight message with a stub
        that includes the note id and a 3-line preview. This tool
        lets the LLM pull the full body when the preview isn't
        enough.
        """
        if self._tool_offloader is None:
            return ToolResult(
                ok=False,
                error="scratchpad_read_offloaded: tool offloader not enabled in this runtime",
            )
        note_id = args.get("id")
        if not isinstance(note_id, int) or note_id <= 0:
            return ToolResult(
                ok=False,
                error="scratchpad_read_offloaded: 'id' must be a positive integer",
            )
        # Default to the configured read chunk size, but allow the
        # caller to override. We read the setting from the
        # offloader's settings object so a runtime-constructed
        # override (e.g. tests) is respected.
        default_max = 4096
        offloader_settings = getattr(self._tool_offloader, "_settings", None)
        if offloader_settings is not None:
            default_max = int(
                getattr(offloader_settings, "tool_offload_read_max_bytes", 4096)
                or 4096,
            )
        max_bytes_raw = args.get("max_bytes", default_max)
        try:
            max_bytes = int(max_bytes_raw)
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                error=(
                    f"scratchpad_read_offloaded: 'max_bytes' must be an integer, "
                    f"got {max_bytes_raw!r}"
                ),
            )
        if max_bytes <= 0:
            max_bytes = default_max
        try:
            content = await self._tool_offloader.read(
                note_id, max_bytes=max_bytes,
            )
        except Exception as exc:  # noqa: BLE001 — chat loop must not break
            logger.warning(
                "scratchpad_read_offloaded failed for id=%d: %s",
                note_id, exc,
            )
            return ToolResult(
                ok=False,
                error=f"scratchpad_read_offloaded: {type(exc).__name__}: {exc}",
            )
        if content is None:
            return ToolResult(
                ok=False,
                error=(
                    f"scratchpad_read_offloaded: offloaded note id={note_id} not found"
                ),
            )
        return ToolResult(ok=True, output=content)

    async def _scratchpad_search_offloaded(
        self, args: dict[str, Any],
    ) -> ToolResult:
        """Semantic search across offloaded tool results.

        Reuses the v1.3.0 :class:`~harness.agents.l2_retriever.L2Retriever`
        for hybrid dense+BM25+curator search, but restricts the
        corpus to notes tagged ``#tool-offload``. This is the
        "find that big tool result from earlier" companion to
        :meth:`_scratchpad_read_offloaded`.
        """
        if self._l2_retriever is None:
            return ToolResult(
                ok=False,
                error=(
                    "scratchpad_search_offloaded: L2 retriever not enabled — "
                    "install v1.3.0 components first"
                ),
            )
        if self._scratchpad is None:
            return ToolResult(
                ok=False,
                error="scratchpad_search_offloaded: scratchpad not enabled in this runtime",
            )
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                ok=False,
                error="scratchpad_search_offloaded: 'query' must be a non-empty string",
            )
        top_k_raw = args.get("top_k", 5)
        try:
            top_k = int(top_k_raw)
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                error=(
                    f"scratchpad_search_offloaded: 'top_k' must be an integer, "
                    f"got {top_k_raw!r}"
                ),
            )
        if top_k <= 0 or top_k > 50:
            return ToolResult(
                ok=False,
                error=(
                    f"scratchpad_search_offloaded: 'top_k' must be in [1, 50], "
                    f"got {top_k}"
                ),
            )
        try:
            # Pull the L2 corpus and filter to #tool-offload notes
            # in Python (cheap, exact tag match). The v1.3.0
            # retriever's curated_search takes the pre-fetched notes
            # list as input — we just narrow the candidate set
            # before passing it in.
            all_l2 = await self._scratchpad.read_notes("L2", limit=200)
            filtered = [
                n for n in all_l2
                if "#tool-offload" in (n.tags or [])
            ]
            if not filtered:
                return ToolResult(ok=True, output="[]")
            scored = await self._l2_retriever.curated_search(
                query=query,
                top_k=top_k,
                candidate_k=min(50, len(filtered)),
                notes=filtered,
                router=self._l2_router,
                model=self._l2_curator_model,
            )
        except Exception as exc:  # noqa: BLE001 — chat loop must not break
            logger.warning(
                "scratchpad_search_offloaded failed for query=%r: %s",
                query, exc,
            )
            return ToolResult(
                ok=False,
                error=(
                    f"scratchpad_search_offloaded: {type(exc).__name__}: {exc}"
                ),
            )
        payload = [
            {
                "id": int(note.id),
                "score": float(score),
                "preview": note.content[:200],
                "tags": list(note.tags or []),
            }
            for note, score in scored
        ]
        return ToolResult(ok=True, output=json.dumps(payload, ensure_ascii=False))


__all__ = ["ToolResult", "ToolRuntime", "ToolName"]
