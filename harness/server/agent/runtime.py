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

ToolName = Literal["read_file", "edit_file", "write_file", "bash", "grep", "glob"]


# === Default tunables ===

DEFAULT_BASH_TIMEOUT = 30  # seconds
MIN_BASH_TIMEOUT = 1
MAX_BASH_TIMEOUT = 300
DEFAULT_GREP_TIMEOUT = 30


class ToolRuntime:
    """Executes tools against a sandboxed ``project_root``.

    Instantiate one runtime per agent session. The runtime is stateless
    beyond the project_root reference.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve(strict=False)

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


__all__ = ["ToolResult", "ToolRuntime", "ToolName"]
