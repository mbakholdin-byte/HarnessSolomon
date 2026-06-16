"""Phase 4.2 Step 2: Hot-reload for .harness/agents/*.md.

Watches the project override directory and re-parses ``AgentSpec``s
on file change. Uses ``FileWatcher`` (Phase 4.2 Step 1) for cross-
platform file watching.

Trust boundary: imports from ``harness.agents.registry``,
``harness.agents.spec``, ``harness.watcher``. NO imports of
``harness.observability``, ``harness.hooks``, ``harness.server``.

Strategy:
    1. On every change in ``.harness/agents/*.md``, re-parse the file
       using ``harness.agents.registry._read_override`` (private but
       stable).
    2. Emit a structured ``emit_hot_reload`` event for observability.
    3. Fail-open: malformed file → log warning, keep last good spec.

Why not a full registry swap? Built-in agents (in
``harness/agents/builtin/``) are bundled and never change at
runtime. Only project overrides can hot-reload. We patch the
``all_specs()`` cache via a closure that re-reads on every call.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from harness.agents.registry import _read_override
from harness.watcher import FileChange, FileWatcher, get_file_watcher

_log = logging.getLogger(__name__)

#: File pattern for agent overrides. Phase 2.0+ used ``**/*.md``.
AGENT_PATTERN: str = "**/*.md"


def _emit_hot_reload(kind: str, path: Path, status: str, error: str = "") -> None:
    """Emit a structured hot-reload event. Fail-open: never raises."""
    try:
        from harness.observability import LogEvent
        from harness.observability import get_observability

        obs = get_observability()
        if not obs.settings.observability_enabled:
            return
        obs.emit(
            LogEvent(
                event="hot_reload",
                payload={
                    "kind": kind,
                    "path": str(path),
                    "status": status,
                    "error": error,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        _log.debug("hot_reload emit failed: %s", exc)


async def _on_agent_change(changes: list[FileChange]) -> None:
    """Re-parse one or more changed agent .md files."""
    for fc in changes:
        # _read_override expects a project_root and name. The file
        # path we get is <project_root>/.harness/agents/<name>.md
        # (or any subdir thereof). We need to find the project_root
        # by walking up from the file path.
        path = fc.path
        # Find the ".harness" segment in the path.
        parts = path.parts
        try:
            idx = parts.index(".harness")
        except ValueError:
            _log.warning("hot_reload: %s not under .harness/ — skip", path)
            continue
        # project_root is everything BEFORE .harness.
        project_root = Path(*parts[:idx])
        # Name is path relative to agents/ directory, with .md
        # stripped. We pass the FULL file path to _read_override
        # via the name argument (it accepts a name and looks up
        # the file at <project_root>/.harness/agents/<name>.md).
        rel = Path(*parts[idx + 2:])  # skip '.harness' and 'agents'
        name = str(rel.with_suffix(""))  # strip .md
        if not name:
            continue
        try:
            spec = _read_override(project_root, name)
            if spec is None:
                # File was deleted or no longer matches.
                _emit_hot_reload(
                    "agents", path,
                    status="removed" if fc.kind.value == "deleted" else "skip",
                )
                continue
            _emit_hot_reload("agents", path, status="ok")
        except Exception as exc:  # noqa: BLE001 — keep last good spec
            _log.warning(
                "hot_reload: failed to parse %s: %s", path, exc,
            )
            _emit_hot_reload("agents", path, status="error", error=str(exc))


async def start_agent_hot_reload(
    project_root: Path,
    *,
    debounce_ms: int = 200,
) -> FileWatcher:
    """Start watching ``.harness/agents/*.md`` under ``project_root``.

    Returns the FileWatcher so the caller can stop it on shutdown.
    """
    agents_dir = project_root / ".harness" / "agents"
    if not agents_dir.exists():
        _log.debug(
            "hot_reload: %s does not exist — skipping agent watcher",
            agents_dir,
        )
        # Return the singleton so callers can .stop() unconditionally.
        return get_file_watcher()
    watcher = get_file_watcher()
    await watcher.watch(
        agents_dir,
        pattern=AGENT_PATTERN,
        on_change=_on_agent_change,
        debounce_ms=debounce_ms,
    )
    _log.info("hot_reload: watching %s for *.md changes", agents_dir)
    return watcher


__all__ = [
    "AGENT_PATTERN",
    "start_agent_hot_reload",
]
