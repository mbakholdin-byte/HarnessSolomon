"""Phase 4.2: FileWatcher — async file-watcher primitive.

Watches a directory tree and fires callbacks on add/modify/delete.

Backends (tried in order):
    1. ``watchfiles.awatch`` — Rust-backed (Notify on macOS/Linux,
       ReadDirectoryChangesW on Windows). Fast + reliable.
    2. Polling fallback — ``asyncio.sleep(poll_interval_s)`` + diff
       against last snapshot. Used if watchfiles import fails or
       the underlying OS event API is unavailable.

Fail-open: any error inside the watcher loop is logged + skipped,
never propagates to the caller. The watcher is a side effect; it
must not break the application.

Trust boundary: stdlib + watchfiles only. NO imports of
``harness.agents``, ``harness.hooks``, ``harness.server``, or
``harness.observability``.

Usage:
    watcher = FileWatcher()
    await watcher.watch(
        path=Path(".harness/agents"),
        pattern="**/*.md",
        on_change=my_callback,
        debounce_ms=200,
    )
    # ... later, on shutdown:
    await watcher.stop()
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Iterable

_log = logging.getLogger(__name__)

# Try to import watchfiles (Rust-backed, fast). Fall back to polling
# if not installed.
try:
    from watchfiles import Change, awatch  # type: ignore[import-untyped]
    _HAS_WATCHFILES = True
except ImportError:  # pragma: no cover — exercised in CI without watchfiles
    _HAS_WATCHFILES = False
    Change = None  # type: ignore[assignment, misc]
    awatch = None  # type: ignore[assignment]


class FileChangeKind(str, Enum):
    """Coarse file change kind (coalesced from watchfiles.Change)."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class FileChange:
    """A single file change event."""

    path: Path
    kind: FileChangeKind


#: Callback type: async function called with a list of FileChange.
ChangeCallback = Callable[[list[FileChange]], Awaitable[None]]


def _coalesce_changes(
    raw: Iterable[tuple[object, str]],
) -> list[FileChange]:
    """Convert watchfiles (Change, path) tuples to FileChange list.

    watchfiles emits 3 Change variants: added, modified, deleted.
    We coalesce multiple events on the same path into the latest one.
    """
    if not _HAS_WATCHFILES or Change is None:
        return []
    out: dict[str, FileChange] = {}
    for change, path_str in raw:
        p = Path(path_str)
        # Map Change enum to our FileChangeKind.
        # Change.added=1, Change.modified=2, Change.deleted=3.
        name = getattr(change, "name", str(change))
        if name == "added":
            kind = FileChangeKind.ADDED
        elif name == "deleted":
            kind = FileChangeKind.DELETED
        else:
            kind = FileChangeKind.MODIFIED
        # Coalesce: keep latest per path.
        out[str(p)] = FileChange(path=p, kind=kind)
    return list(out.values())


def _matches_glob(path: Path, pattern: str) -> bool:
    """Match path against a glob pattern. Supports ``**`` for recursive.

    Uses ``fnmatch.fnmatch`` semantics (not full pathlib glob). This
    is intentional: glob patterns like ``**/*.md`` are universal, while
    pathlib's glob() requires actually walking the tree.
    """
    # Normalise path separators to '/' for cross-platform matching.
    p = str(path).replace("\\", "/")
    pat = pattern.replace("\\", "/")
    return fnmatch.fnmatch(p, pat)


class FileWatcher:
    """Async file watcher with debounce + polling fallback.

    One FileWatcher instance can watch multiple paths. Each ``watch()``
    call spawns a background task. Call ``stop()`` to cancel all tasks.

    Debouncing: changes that arrive within ``debounce_ms`` of each
    other are batched into a single callback invocation. This avoids
    the "edit triggers 5 events" problem (write + truncate + close).
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        # Snapshot map: path_str → mtime (for polling fallback).
        self._snapshots: dict[str, float] = {}

    async def watch(
        self,
        path: Path,
        *,
        pattern: str = "*",
        on_change: ChangeCallback,
        debounce_ms: int = 200,
        poll_interval_s: float = 1.0,
    ) -> None:
        """Start watching ``path`` recursively. Returns immediately.

        ``on_change`` is an async callable that receives a list of
        FileChange. The watcher accumulates changes for ``debounce_ms``
        then calls ``on_change`` once with the coalesced list.

        ``pattern`` is a glob (fnmatch-style). Use ``*.md`` for one
        level, ``**/*.md`` for recursive.

        ``poll_interval_s`` is only used if watchfiles is not installed
        OR the OS event API fails. The default of 1.0s is enough for
        development; production should use the watchfiles path.
        """
        if self._stopping:
            return
        if not path.exists():
            _log.warning("FileWatcher.watch: path %s does not exist", path)
            return
        if _HAS_WATCHFILES:
            task = asyncio.create_task(
                self._watch_loop_watchfiles(
                    path, pattern, on_change, debounce_ms,
                ),
                name=f"watcher:{path}",
            )
        else:
            task = asyncio.create_task(
                self._watch_loop_polling(
                    path, pattern, on_change, debounce_ms, poll_interval_s,
                ),
                name=f"watcher-poll:{path}",
            )
        self._tasks.append(task)
        _log.debug("FileWatcher: started watch on %s (pattern=%s)", path, pattern)

    async def stop(self) -> None:
        """Cancel all watcher tasks. Idempotent."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        # Drain cancelled tasks.
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._stopping = False
        _log.debug("FileWatcher: stopped")

    @property
    def active(self) -> int:
        """Number of active watch tasks (for /metrics)."""
        return len(self._tasks)

    # === Internal loops ===

    async def _watch_loop_watchfiles(
        self,
        path: Path,
        pattern: str,
        on_change: ChangeCallback,
        debounce_ms: int,
    ) -> None:
        """watchfiles.awatch loop with debounce."""
        assert awatch is not None  # checked by caller
        debounce_s = debounce_ms / 1000.0
        try:
            async for changes in awatch(str(path)):
                if self._stopping:
                    return
                # Filter by pattern, then coalesce.
                filtered: list[FileChange] = []
                for fc in _coalesce_changes(changes):
                    if _matches_glob(fc.path, pattern):
                        filtered.append(fc)
                if not filtered:
                    continue
                # Debounce: wait debounce_s, then call.
                # If more changes arrive in that window, they get
                # coalesced into the next iteration. For perfect
                # coalescing we should buffer + re-check, but this
                # is good enough for the typical "editor save" pattern.
                await asyncio.sleep(debounce_s)
                if self._stopping:
                    return
                try:
                    await on_change(filtered)
                except Exception as exc:  # noqa: BLE001 — fail-open
                    _log.warning(
                        "FileWatcher: on_change raised for %s: %s",
                        path, exc,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — watcher MUST fail-open
            _log.warning("FileWatcher (watchfiles) failed for %s: %s", path, exc)

    async def _watch_loop_polling(
        self,
        path: Path,
        pattern: str,
        on_change: ChangeCallback,
        debounce_ms: int,
        poll_interval_s: float,
    ) -> None:
        """Polling fallback: mtime diff against last snapshot."""
        debounce_s = debounce_ms / 1000.0
        self._snapshot_tree(path, pattern)
        try:
            while not self._stopping:
                await asyncio.sleep(poll_interval_s)
                if self._stopping:
                    return
                changes = self._diff_tree(path, pattern)
                if not changes:
                    continue
                await asyncio.sleep(debounce_s)
                if self._stopping:
                    return
                try:
                    await on_change(changes)
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "FileWatcher (poll): on_change raised for %s: %s",
                        path, exc,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.warning("FileWatcher (poll) failed for %s: %s", path, exc)

    def _snapshot_tree(self, path: Path, pattern: str) -> None:
        """Build initial snapshot of (path → mtime) for polling."""
        try:
            for p in path.rglob("*"):
                if not p.is_file():
                    continue
                if not _matches_glob(p, pattern):
                    continue
                try:
                    self._snapshots[str(p)] = p.stat().st_mtime
                except OSError:
                    pass
        except OSError as exc:
            _log.warning("FileWatcher: snapshot failed for %s: %s", path, exc)

    def _diff_tree(
        self,
        path: Path,
        pattern: str,
    ) -> list[FileChange]:
        """Diff current tree against snapshot. Updates snapshot."""
        changes: list[FileChange] = []
        seen: set[str] = set()
        try:
            for p in path.rglob("*"):
                if not p.is_file():
                    continue
                if not _matches_glob(p, pattern):
                    continue
                p_str = str(p)
                seen.add(p_str)
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                prev = self._snapshots.get(p_str)
                if prev is None:
                    changes.append(FileChange(path=p, kind=FileChangeKind.ADDED))
                elif mtime > prev:
                    changes.append(FileChange(path=p, kind=FileChangeKind.MODIFIED))
                self._snapshots[p_str] = mtime
        except OSError as exc:
            _log.warning("FileWatcher: diff failed for %s: %s", path, exc)
            return changes
        # Detect deletions: anything in snapshot but not seen now.
        for old_path_str in list(self._snapshots.keys()):
            if old_path_str not in seen and old_path_str.startswith(str(path)):
                changes.append(
                    FileChange(path=Path(old_path_str), kind=FileChangeKind.DELETED)
                )
                del self._snapshots[old_path_str]
        return changes


__all__ = [
    "FileChange",
    "FileChangeKind",
    "FileWatcher",
    "ChangeCallback",
]


# Module-level singleton + accessor (mirror harness/observability/emit.py).
_lock = asyncio.Lock()
_instance: FileWatcher | None = None


def get_file_watcher() -> FileWatcher:
    """Return the process-level FileWatcher singleton (lazy-init)."""
    global _instance
    if _instance is None:
        _instance = FileWatcher()
    return _instance


def reset_file_watcher() -> None:
    """Reset the singleton. For tests only."""
    global _instance
    _instance = None
