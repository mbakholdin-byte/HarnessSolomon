"""Phase 4.2 Step 2: Hot-reload for .harness/agents/*.md.
Phase 4.2+ v1.9.0: Hot-reload for built-in agents in
``harness/agents/builtin/*.md``.

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

Built-in hot-reload (v1.9.0):
    The same strategy extends to the bundled ``harness/agents/builtin/*.md``
    files. The built-in directory is resolved via ``importlib.resources``
    and then converted to a real ``Path`` for the file watcher. Built-in
    specs are read on every call to :func:`all_specs` (lazy), so no
    in-memory cache swap is needed — the next ``all_specs()`` invocation
    will pick up the new content. This watcher exists primarily to
    *emit* a hot_reload event for observability and to fail loudly on
    malformed built-ins (which the user can't recover from at runtime).
"""
from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from harness.agents.registry import BUILTIN_DIR_RESOURCE, _read_override
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


# === Phase 4.2+ v1.9.0: Built-in agent hot-reload ===


def _builtin_dir() -> Path | None:
    """Resolve ``harness/agents/builtin/`` to a real :class:`Path`.

    Uses :mod:`importlib.resources` (Python 3.9+) for package-data
    access. Returns ``None`` if the directory cannot be resolved
    (e.g. unusual packaging — happens only in tests with stub
    packages). The caller treats ``None`` as "skip — no built-in dir".

    Why convert to Path? The :class:`FileWatcher` only knows about
    real filesystem paths. ``importlib.resources`` returns a
    :class:`importlib.abc.Traversable` (or a ``MultiplexedPath`` in
    editable installs) which doesn't expose :meth:`Path.rglob` and
    doesn't implement :func:`os.fspath`.

    Conversion strategy:
        - For ``MultiplexedPath`` (editable installs), use ``_paths[0]``
          which is a real :class:`pathlib.Path`.
        - For other Traversables, walk via :meth:`iterdir` and pick
          the first concrete :class:`Path` (falls back to None if
          all entries are non-fspath).
        - Last resort: try ``os.fspath`` directly.
    """
    try:
        traversable = resources.files(BUILTIN_DIR_RESOURCE)
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    if not hasattr(traversable, "is_dir") or not traversable.is_dir():
        return None
    # MultiplexedPath: editable install (the common dev case).
    if hasattr(traversable, "_paths"):
        try:
            first = traversable._paths[0]
            if isinstance(first, Path):
                return first if first.is_dir() else None
        except (IndexError, AttributeError):
            pass
    # Fallback: try os.fspath on the traversable itself.
    import os
    try:
        p = Path(os.fspath(traversable))
        return p if p.is_dir() else None
    except (TypeError, OSError, ValueError):
        pass
    # Last fallback: search the iterdir children for the first
    # fspath-compatible Path. If we find one, its parent is our dir.
    for child in traversable.iterdir():
        try:
            child_path = Path(os.fspath(child))
            if child_path.parent.is_dir():
                return child_path.parent
        except (TypeError, OSError, ValueError):
            continue
    return None


async def _on_builtin_change(changes: list[FileChange]) -> None:
    """Re-validate a built-in agent .md on change.

    Built-in specs are read lazily by :func:`harness.agents.registry.all_specs`,
    so no explicit registry swap is needed. This handler:
    1. Validates the new content (re-parse via ``_read_builtin``).
    2. Emits a ``hot_reload`` event for observability.
    3. Logs and skips on parse errors — the next ``all_specs()`` call
       will retry naturally (built-ins are bundled; the user can't
       hot-fix a broken spec anyway, but a transient parse error
       during save shouldn't crash the watcher).
    """
    from harness.agents.registry import _read_builtin  # local import to avoid cycle
    from harness.agents.spec import FrontmatterParseError

    for fc in changes:
        path = fc.path
        # Verify path is under harness/agents/builtin/.
        # FileWatcher passes the absolute path, so we check suffix.
        if "builtin" not in path.parts:
            _log.warning("hot_reload: %s not under builtin/ — skip", path)
            continue
        if fc.kind.value == "deleted":
            _log.warning(
                "hot_reload: built-in %s deleted — registry will fall back "
                "to last good spec via read_builtin (or skip if gone)",
                path,
            )
            _emit_hot_reload("builtin_agents", path, status="removed")
            continue
        # Re-validate. The name is the file stem.
        name = path.stem
        try:
            spec = _read_builtin(name)
            if spec is None:
                _log.warning(
                    "hot_reload: built-in %s no longer parseable", path,
                )
                _emit_hot_reload(
                    "builtin_agents", path, status="error",
                    error="not parseable",
                )
                continue
            _emit_hot_reload("builtin_agents", path, status="ok")
        except FrontmatterParseError as exc:
            _log.warning(
                "hot_reload: failed to parse built-in %s: %s", path, exc,
            )
            _emit_hot_reload(
                "builtin_agents", path, status="error", error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — keep last good spec
            _log.warning(
                "hot_reload: built-in %s unexpected error: %s", path, exc,
            )
            _emit_hot_reload(
                "builtin_agents", path, status="error", error=str(exc),
            )


async def start_builtin_agent_hot_reload(
    *,
    debounce_ms: int = 200,
) -> FileWatcher:
    """Start watching the built-in ``harness/agents/builtin/*.md`` directory.

    Built-in specs are read lazily by :func:`all_specs`, so this
    watcher exists mainly to emit observability events and surface
    parse errors early. Useful in dev when editing
    ``harness/agents/builtin/*.md`` directly.

    Returns:
        The FileWatcher singleton so the caller can stop it on shutdown.

    Note:
        If the built-in directory cannot be resolved (unusual
        packaging, frozen binaries), returns the watcher singleton
        without spawning a task. Caller can ``stop()`` unconditionally.
    """
    builtin_dir = _builtin_dir()
    if builtin_dir is None:
        _log.debug(
            "hot_reload: built-in dir not resolvable — skipping watcher",
        )
        return get_file_watcher()
    if not builtin_dir.exists():
        _log.debug(
            "hot_reload: %s does not exist — skipping built-in watcher",
            builtin_dir,
        )
        return get_file_watcher()
    watcher = get_file_watcher()
    await watcher.watch(
        builtin_dir,
        pattern=AGENT_PATTERN,
        on_change=_on_builtin_change,
        debounce_ms=debounce_ms,
    )
    _log.info("hot_reload: watching %s for *.md changes", builtin_dir)
    return watcher


__all__ = [
    "AGENT_PATTERN",
    "start_agent_hot_reload",
    "start_builtin_agent_hot_reload",
]
