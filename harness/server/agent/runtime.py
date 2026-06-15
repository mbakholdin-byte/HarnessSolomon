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
]


# === Default tunables ===

DEFAULT_BASH_TIMEOUT = 30  # seconds
MIN_BASH_TIMEOUT = 1
MAX_BASH_TIMEOUT = 300
DEFAULT_GREP_TIMEOUT = 30


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

    # --- dispatcher ---

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Dispatch to the right handler. Unknown tool → error result."""
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
            else:
                result = ToolResult(ok=False, error=f"unknown tool: {name!r}")
        except Exception as exc:  # noqa: BLE001 — top-level safety net
            logger.exception("tool %s raised", name)
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if result.duration_ms is None:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            result = result.model_copy(update={"duration_ms": elapsed_ms})
        return result

    # --- file tools ---

    async def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path_str = args.get("path")
        if not isinstance(path_str, str) or not path_str:
            return ToolResult(ok=False, error="read_file: 'path' is required")

        resolved = resolve_safe_path(path_str, self.project_root)
        if resolved is None:
            return ToolResult(
                ok=False, error=f"path outside project_root: {path_str!r}"
            )

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

        # 1. Safety check FIRST — before spawning any process.
        denied_pattern = is_bash_denied(command)
        if denied_pattern is not None:
            logger.warning("bash denied by safety pattern: %s", denied_pattern)
            return ToolResult(
                ok=False,
                error=f"denied: matches safety pattern {denied_pattern!r}",
            )

        # 2. Spawn via shell. We accept the security tradeoff here because
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

        # Resolve path. If absent, use project_root.
        if path_str is None or path_str == "":
            base = self.project_root
        else:
            resolved = resolve_safe_path(path_str, self.project_root)
            if resolved is None:
                return ToolResult(
                    ok=False, error=f"path outside project_root: {path_str!r}"
                )
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

        if path_str is None or path_str == "":
            base = self.project_root
        else:
            resolved = resolve_safe_path(path_str, self.project_root)
            if resolved is None:
                return ToolResult(
                    ok=False, error=f"path outside project_root: {path_str!r}"
                )
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


__all__ = ["ToolResult", "ToolRuntime", "ToolName"]
