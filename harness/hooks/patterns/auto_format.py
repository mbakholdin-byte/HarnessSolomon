#!/usr/bin/env python3
"""Phase 4.10 Task A: auto_format hook pattern (subprocess transport).

Standalone script — reads a JSON HookContext from stdin, optionally
runs ``ruff format`` on the written file, and exits 0 (never blocks).

Stdin JSON shape (subset of HookContext):
    {
        "event": "PostToolUse",
        "session_id": "...",
        "agent_id": "...",
        "payload": {
            "tool_name": "write_file" | "edit_file",
            "arguments": {"path": "src/module.py", ...},
            "ok": true | false,
            ...
        }
    }

Contract:
    - If event != PostToolUse → exit 0 (no-op).
    - If tool_name not in {write_file, edit_file} → exit 0.
    - If payload.ok is falsy → exit 0 (skip formatting on failed writes).
    - If path does not end in ``.py`` → exit 0.
    - Otherwise: ``subprocess.run(["ruff", "format", path], timeout=4)``.
    - ANY failure (timeout, FileNotFoundError, non-zero exit) is logged
      to stderr but NEVER raised — the hook always exits 0.

Trust boundary: stdlib + subprocess only. No ``harness.*`` imports.
This file must remain runnable as ``python harness/hooks/patterns/auto_format.py``
outside the harness package (e.g. from the hook subprocess transport).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

# Tools whose output is a file we may want to format.
_FORMATTABLE_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})

# Hard ceiling for the ruff subprocess (seconds). The hook spec's
# timeout_ms is enforced separately by the harness subprocess transport;
# this is a belt-and-suspenders guard so a wedged ruff cannot pin a worker.
_RUFF_TIMEOUT_S: float = 4.0


def _log_err(msg: str) -> None:
    """Write a single line to stderr (best-effort, never raises)."""
    try:
        sys.stderr.write(f"auto_format: {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — stderr is best-effort
        pass


def _extract_path(payload: dict[str, Any]) -> str:
    """Return the file path from a PostToolUse payload, or '' if absent."""
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        path = arguments.get("path")
        if isinstance(path, str):
            return path
    # Some callers flatten args into the payload directly — be lenient.
    path = payload.get("path")
    return path if isinstance(path, str) else ""


def _should_format(data: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether to format; return (should_format, path_or_reason)."""
    # Event gate — only PostToolUse is meaningful here.
    if data.get("event") != "PostToolUse":
        return False, "non-PostToolUse event"
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return False, "payload not a dict"
    # Skip on failed tool calls — formatting a half-written file is harmful.
    if not payload.get("ok"):
        return False, "tool call reported not-ok"
    tool_name = payload.get("tool_name", "")
    if tool_name not in _FORMATTABLE_TOOLS:
        return False, f"tool {tool_name!r} not formattable"
    path = _extract_path(payload)
    if not path:
        return False, "no path in payload"
    if not path.endswith(".py"):
        return False, f"path {path!r} is not a .py file"
    return True, path


def _run_ruff(path: str) -> None:
    """Invoke ``ruff format`` on path. Swallows all errors."""
    try:
        subprocess.run(
            ["ruff", "format", path],
            timeout=_RUFF_TIMEOUT_S,
            check=False,  # we don't care about ruff's exit code
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        _log_err("ruff not found on PATH — skipping")
    except subprocess.TimeoutExpired:
        _log_err(f"ruff format timed out after {_RUFF_TIMEOUT_S}s on {path}")
    except Exception as exc:  # noqa: BLE001 — never propagate
        _log_err(f"ruff format failed on {path}: {type(exc).__name__}: {exc}")


def main() -> int:
    """Entry point. Returns 0 unconditionally (hook never blocks)."""
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log_err(f"invalid JSON on stdin: {exc}")
        return 0
    if not isinstance(data, dict):
        _log_err("stdin JSON is not an object")
        return 0
    should, path_or_reason = _should_format(data)
    if not should:
        # Non-blocking skip — path_or_reason is a human-readable reason.
        return 0
    _run_ruff(path_or_reason)
    return 0


if __name__ == "__main__":
    # Ensure deterministic exit code regardless of how we were invoked.
    sys.exit(main())
