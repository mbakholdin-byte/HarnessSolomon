"""Phase 4.0: Subprocess transport for hooks (JSON via stdin/stdout).

A subprocess hook is a script that:
    1. Reads a JSON ``HookContext`` from stdin.
    2. Performs its work.
    3. Writes a JSON ``HookDecision`` to stdout.
    4. Exits with code:
        - ``0`` = success (decision is taken from stdout JSON)
        - ``2`` = block (stderr may have a human-readable reason)
        - other = error (runner treats as allow, fail-open)

This mirrors Claude Code's CC hook protocol. The runner enforces a
hard timeout via ``asyncio.wait_for``; on timeout the process group
is killed (Unix: ``os.setsid`` + ``os.killpg``; Windows:
``subprocess.CREATE_NEW_PROCESS_GROUP`` + ``CTRL_BREAK_EVENT``).

Trust boundary: stdlib only (``subprocess``, ``asyncio``, ``json``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger(__name__)


async def invoke_subprocess_hook(
    script_path: str,
    context: HookContext,
    *,
    timeout_ms: int,
) -> HookDecision:
    """Run a subprocess hook and return its decision.

    Args:
        script_path: Absolute or relative path to the script. The script
            receives a JSON ``HookContext`` on stdin and must write a
            JSON ``HookDecision`` to stdout.
        context: The context to pass to the hook.
        timeout_ms: Hard timeout. On timeout, the process group is
            killed and the runner returns ``allow`` (fail-open).

    Returns:
        ``HookDecision`` with decision, hook_id=``subprocess.<script>``,
        and ``error`` populated on any failure.
    """
    hook_id = f"subprocess.{os.path.basename(script_path)}"
    start = time.monotonic()
    payload = {
        "event": context.event,
        "session_id": context.session_id,
        "agent_id": context.agent_id,
        "payload": context.payload,
        "ts": context.ts,
        "request_id": context.request_id,
        "recursion_depth": context.recursion_depth,
        "event_stack": list(context.event_stack),
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    creationflags = 0
    preexec_fn = None
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP = 0x00000200; CTRL_BREAK_EVENT required
        # to kill the entire process group on Windows.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        preexec_fn = os.setsid  # type: ignore[assignment]

    # Pre-check: does the script exist? On Windows, asyncio.create_subprocess_exec
    # does NOT raise FileNotFoundError; it spawns the interpreter which then
    # exits with code 2 (block by CC protocol convention). We want a clean
    # fail-open in either case, so we check the path up front.
    if not os.path.isfile(script_path):
        duration_ms = (time.monotonic() - start) * 1000.0
        return HookDecision(
            decision="allow",
            hook_id=hook_id,
            duration_ms=duration_ms,
            error=f"script not found: {script_path}",
        )

    proc: asyncio.subprocess.Process | None = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )
        except (FileNotFoundError, OSError, PermissionError) as e:
            duration_ms = (time.monotonic() - start) * 1000.0
            return HookDecision(
                decision="allow",
                hook_id=hook_id,
                duration_ms=duration_ms,
                error=f"script not executable: {e!r}",
            )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=payload_bytes),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000.0
            await _kill_process_group(proc)
            logger.warning(
                "Subprocess hook %s timed out after %dms", script_path, timeout_ms
            )
            return HookDecision(
                decision="allow",  # fail-open
                hook_id=hook_id,
                duration_ms=duration_ms,
                error=f"subprocess timeout after {timeout_ms}ms",
            )
        duration_ms = (time.monotonic() - start) * 1000.0
        return_code = proc.returncode
        if return_code == 2:
            # Block decision (CC protocol). The reason is on stderr.
            reason = stderr_bytes.decode("utf-8", errors="replace").strip()
            return HookDecision(
                decision="block",
                hook_id=hook_id,
                duration_ms=duration_ms,
                output={"reason": reason} if reason else {},
            )
        if return_code != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            return HookDecision(
                decision="allow",  # fail-open
                hook_id=hook_id,
                duration_ms=duration_ms,
                error=f"subprocess exited with code {return_code}: {stderr_text[:200]}",
            )
        # Exit 0: parse JSON decision from stdout.
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not stdout_text:
            return HookDecision(
                decision="allow",
                hook_id=hook_id,
                duration_ms=duration_ms,
                error="subprocess exited 0 with empty stdout",
            )
        try:
            data: dict[str, Any] = json.loads(stdout_text)
        except json.JSONDecodeError as e:
            return HookDecision(
                decision="allow",
                hook_id=hook_id,
                duration_ms=duration_ms,
                error=f"invalid JSON from subprocess: {e}",
            )
        decision_str = data.get("decision", "allow")
        if decision_str not in ("allow", "block", "modify"):
            decision_str = "allow"
        return HookDecision(
            decision=decision_str,  # type: ignore[arg-type]
            hook_id=hook_id,
            duration_ms=duration_ms,
            output=dict(data.get("output", {})),
            error=str(data.get("error", "")),
        )
    except Exception as e:  # noqa: BLE001
        duration_ms = (time.monotonic() - start) * 1000.0
        logger.warning(
            "Subprocess hook %s raised %s: %s", script_path, type(e).__name__, e
        )
        return HookDecision(
            decision="allow",
            hook_id=hook_id,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
        )


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of the process group rooted at ``proc``."""
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            import signal

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[arg-type]
            except (ProcessLookupError, PermissionError):
                proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    # Give the process a moment to die, then force-kill.
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["invoke_subprocess_hook"]
