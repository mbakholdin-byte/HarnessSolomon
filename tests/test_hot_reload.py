"""Phase 4.2: Tests for FileWatcher + hot-reload.

Strategy:
    1. FileWatcher tests use a tmp_path and a short polling
       interval. They sleep 1.5s after writes to allow the
       watcher to fire.
    2. Hot-reload tests for agents/hooks exercise the parse
       functions and the start_*_hot_reload wrappers without
       requiring a real file system event to propagate (the
       polling fallback catches them within 1.5s).

All tests are isolated — each gets its own tmp_path and
resets the global FileWatcher singleton.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Iterator

import pytest

from harness.watcher import (
    FileChange,
    FileChangeKind,
    FileWatcher,
    get_file_watcher,
    reset_file_watcher,
)


@pytest.fixture(autouse=True)
def reset_singleton() -> None:
    """Reset the FileWatcher singleton before each test.

    Done as a setup-only fixture (no yield/cleanup) because
    pytest-asyncio's event loop is per-function, and a singleton
    created in one test must not leak to the next.
    """
    reset_file_watcher()
    return None


# === FileWatcher tests (Step 1) ===


class TestFileChangeBasics:
    def test_file_change_kind_enum(self) -> None:
        assert FileChangeKind.ADDED.value == "added"
        assert FileChangeKind.MODIFIED.value == "modified"
        assert FileChangeKind.DELETED.value == "deleted"

    def test_file_change_dataclass(self) -> None:
        fc = FileChange(path=Path("/tmp/x"), kind=FileChangeKind.ADDED)
        assert fc.path == Path("/tmp/x")
        assert fc.kind == FileChangeKind.ADDED


class TestGlobMatching:
    """Phase 4.2 Step 1: glob pattern matching for watcher filters."""

    def test_simple_match(self) -> None:
        from harness.watcher import _matches_glob
        assert _matches_glob(Path("agents/foo.md"), "*.md")
        assert not _matches_glob(Path("agents/foo.txt"), "*.md")

    def test_double_star_recursive(self) -> None:
        from harness.watcher import _matches_glob
        assert _matches_glob(Path("agents/sub/foo.md"), "**/*.md")
        assert _matches_glob(Path("agents/foo.md"), "**/*.md")
        assert not _matches_glob(Path("agents/foo.txt"), "**/*.md")

    def test_double_star_prefix(self) -> None:
        from harness.watcher import _matches_glob
        assert _matches_glob(Path("a/b/c.txt"), "a/**")
        # Note: fnmatch is not full glob; we use simple *.
        assert _matches_glob(Path("anything.txt"), "*.txt")


class TestFileWatcherLifecycle:
    @pytest.mark.asyncio
    async def test_watch_nonexistent_path_no_crash(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        # Path doesn't exist — should log warning, not raise.
        async def noop(changes: list[FileChange]) -> None:
            pass
        await watcher.watch(
            tmp_path / "does_not_exist",
            pattern="*.md",
            on_change=noop,
        )
        assert watcher.active == 0  # nothing started
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_watch_empty_dir_starts_task(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        empty = tmp_path / "empty"
        empty.mkdir()
        async def noop(changes: list[FileChange]) -> None:
            pass
        await watcher.watch(empty, pattern="*.md", on_change=noop)
        assert watcher.active == 1
        await watcher.stop()
        assert watcher.active == 0

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        (tmp_path / "x").mkdir()
        async def noop(changes: list[FileChange]) -> None:
            pass
        await watcher.watch(tmp_path / "x", pattern="*.md", on_change=noop)
        await watcher.stop()
        await watcher.stop()  # second call should be safe
        assert watcher.active == 0


class TestFileWatcherPollingFallback:
    """Test the polling path (works without watchfiles having events).

    We force the polling branch by using a short debounce + poll interval.
    """

    @pytest.mark.asyncio
    async def test_polling_detects_new_file(self, tmp_path: Path) -> None:
        # Use polling directly: create a manual FileWatcher and
        # call its internal _watch_loop_polling.
        watcher = FileWatcher()
        subdir = tmp_path / "agents"
        subdir.mkdir()
        received: list[list[FileChange]] = []

        async def on_change(changes: list[FileChange]) -> None:
            received.append(changes)

        # Start a polling loop with very short interval.
        task = asyncio.create_task(
            watcher._watch_loop_polling(
                subdir, "*.md", on_change, debounce_ms=50, poll_interval_s=0.1,
            )
        )
        # Give the loop a moment to take its first snapshot.
        await asyncio.sleep(0.2)
        # Add a new file.
        (subdir / "new.md").write_text("# Hello", encoding="utf-8")
        # Wait for poll + debounce.
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # We should have seen at least one ADDED event.
        all_changes = [c for batch in received for c in batch]
        assert any(c.path.name == "new.md" and c.kind == FileChangeKind.ADDED for c in all_changes)

    @pytest.mark.asyncio
    async def test_polling_detects_modify(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        subdir = tmp_path / "agents"
        subdir.mkdir()
        target = subdir / "foo.md"
        target.write_text("v1", encoding="utf-8")
        received: list[list[FileChange]] = []

        async def on_change(changes: list[FileChange]) -> None:
            received.append(changes)

        task = asyncio.create_task(
            watcher._watch_loop_polling(
                subdir, "*.md", on_change, debounce_ms=50, poll_interval_s=0.1,
            )
        )
        await asyncio.sleep(0.2)
        # Modify.
        target.write_text("v2", encoding="utf-8")
        # Bump mtime explicitly to ensure > 1s resolution on Windows.
        import os
        import time
        new_mtime = time.time() + 2
        os.utime(target, (new_mtime, new_mtime))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        all_changes = [c for batch in received for c in batch]
        assert any(c.path.name == "foo.md" and c.kind == FileChangeKind.MODIFIED for c in all_changes)

    @pytest.mark.asyncio
    async def test_polling_detects_delete(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        subdir = tmp_path / "agents"
        subdir.mkdir()
        target = subdir / "foo.md"
        target.write_text("x", encoding="utf-8")
        received: list[list[FileChange]] = []

        async def on_change(changes: list[FileChange]) -> None:
            received.append(changes)

        task = asyncio.create_task(
            watcher._watch_loop_polling(
                subdir, "*.md", on_change, debounce_ms=50, poll_interval_s=0.1,
            )
        )
        await asyncio.sleep(0.2)
        target.unlink()
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        all_changes = [c for batch in received for c in batch]
        assert any(c.path.name == "foo.md" and c.kind == FileChangeKind.DELETED for c in all_changes)

    @pytest.mark.asyncio
    async def test_polling_ignores_non_matching(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        subdir = tmp_path / "agents"
        subdir.mkdir()
        received: list[list[FileChange]] = []

        async def on_change(changes: list[FileChange]) -> None:
            received.append(changes)

        task = asyncio.create_task(
            watcher._watch_loop_polling(
                subdir, "*.md", on_change, debounce_ms=50, poll_interval_s=0.1,
            )
        )
        await asyncio.sleep(0.2)
        (subdir / "ignored.txt").write_text("x", encoding="utf-8")
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Should be empty (no .md files changed).
        all_changes = [c for batch in received for c in batch]
        assert all_changes == []


class TestFileWatcherSnapshot:
    """Test the snapshot + diff helpers directly."""

    def test_snapshot_tree(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        watcher._snapshot_tree(tmp_path, "*.md")
        assert len(watcher._snapshots) == 1
        assert any(k.endswith("a.md") for k in watcher._snapshots)

    def test_diff_tree_detects_added(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        watcher._snapshot_tree(tmp_path, "*.md")
        (tmp_path / "new.md").write_text("x", encoding="utf-8")
        changes = watcher._diff_tree(tmp_path, "*.md")
        assert any(c.path.name == "new.md" and c.kind == FileChangeKind.ADDED for c in changes)

    def test_diff_tree_detects_deleted(self, tmp_path: Path) -> None:
        watcher = FileWatcher()
        target = tmp_path / "foo.md"
        target.write_text("x", encoding="utf-8")
        watcher._snapshot_tree(tmp_path, "*.md")
        target.unlink()
        changes = watcher._diff_tree(tmp_path, "*.md")
        assert any(c.path.name == "foo.md" and c.kind == FileChangeKind.DELETED for c in changes)


class TestSingleton:
    def test_get_file_watcher_singleton(self) -> None:
        a = get_file_watcher()
        b = get_file_watcher()
        assert a is b

    def test_reset_file_watcher_clears(self) -> None:
        a = get_file_watcher()
        reset_file_watcher()
        b = get_file_watcher()
        assert a is not b


# === Hook hot-reload tests (Step 3) ===


class TestParseHookFile:
    def test_parse_single_object(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import _parse_hook_file
        path = tmp_path / "single.json"
        path.write_text(json.dumps({
            "hook_id": "h1",
            "event": "PreToolUse",
            "transport": "builtin",
            "matcher": "tool_name=bash",
            "timeout_ms": 1000,
            "enabled": True,
            "priority": 100,
        }), encoding="utf-8")
        specs = _parse_hook_file(path)
        assert len(specs) == 1
        assert specs[0].hook_id == "h1"
        assert specs[0].event.value == "PreToolUse"

    def test_parse_list(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import _parse_hook_file
        path = tmp_path / "multi.json"
        path.write_text(json.dumps([
            {"hook_id": "h1", "event": "PreToolUse", "transport": "builtin"},
            {"hook_id": "h2", "event": "PostToolUse", "transport": "builtin"},
        ]), encoding="utf-8")
        specs = _parse_hook_file(path)
        assert len(specs) == 2
        assert {s.hook_id for s in specs} == {"h1", "h2"}

    def test_parse_missing_required_field_raises(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import _parse_hook_file
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"event": "PreToolUse"}), encoding="utf-8")
        with pytest.raises(ValueError, match="missing required field"):
            _parse_hook_file(path)

    def test_parse_unknown_event_raises(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import _parse_hook_file
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({
            "hook_id": "h1",
            "event": "NotARealEvent",
            "transport": "builtin",
        }), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown event"):
            _parse_hook_file(path)

    def test_parse_non_object_raises(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import _parse_hook_file
        path = tmp_path / "bad.json"
        path.write_text("\"just a string\"", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON object or list"):
            _parse_hook_file(path)

    def test_parse_with_optional_fields(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import _parse_hook_file
        path = tmp_path / "ok.json"
        path.write_text(json.dumps({
            "hook_id": "h1",
            "event": "PreToolUse",
            "transport": "builtin",
        }), encoding="utf-8")
        specs = _parse_hook_file(path)
        assert specs[0].matcher == ""
        assert specs[0].timeout_ms == 3000  # default
        assert specs[0].enabled is True  # default
        assert specs[0].priority == 100  # default


class TestHotReloadAgentsWiring:
    @pytest.mark.asyncio
    async def test_start_skips_if_dir_missing(self, tmp_path: Path) -> None:
        from harness.agents.hot_reload import start_agent_hot_reload
        # No .harness/agents dir.
        watcher = await start_agent_hot_reload(tmp_path)
        # Returns the singleton but no active tasks.
        assert watcher.active == 0

    @pytest.mark.asyncio
    async def test_start_starts_watcher_if_dir_exists(self, tmp_path: Path) -> None:
        from harness.agents.hot_reload import start_agent_hot_reload
        agents_dir = tmp_path / ".harness" / "agents"
        agents_dir.mkdir(parents=True)
        watcher = await start_agent_hot_reload(tmp_path)
        assert watcher.active == 1
        await watcher.stop()


class TestHotReloadHooksWiring:
    @pytest.mark.asyncio
    async def test_start_skips_if_dir_missing(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import start_hook_hot_reload
        from harness.hooks.registry import HookRegistry
        registry = HookRegistry()
        watcher = await start_hook_hot_reload(registry, tmp_path)
        assert watcher.active == 0

    @pytest.mark.asyncio
    async def test_start_starts_watcher_if_dir_exists(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import start_hook_hot_reload
        from harness.hooks.registry import HookRegistry
        hooks_dir = tmp_path / ".harness" / "hooks"
        hooks_dir.mkdir(parents=True)
        registry = HookRegistry()
        watcher = await start_hook_hot_reload(registry, tmp_path)
        assert watcher.active == 1
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_modify_file_reparses_into_registry(self, tmp_path: Path) -> None:
        """End-to-end: write hook .json → registry contains the spec."""
        from harness.hooks.events import EventType
        from harness.hooks.hot_reload import start_hook_hot_reload
        from harness.hooks.registry import HookRegistry
        from harness.watcher import reset_file_watcher
        reset_file_watcher()  # ensure clean singleton for this test
        hooks_dir = tmp_path / ".harness" / "hooks"
        hooks_dir.mkdir(parents=True)
        registry = HookRegistry()
        watcher = await start_hook_hot_reload(
            registry, tmp_path, debounce_ms=50,
        )
        try:
            # Give the polling loop time to take its first snapshot.
            await asyncio.sleep(0.3)
            # Write a hook spec.
            (hooks_dir / "test.json").write_text(json.dumps({
                "hook_id": "test-1",
                "event": "PreToolUse",
                "transport": "builtin",
            }), encoding="utf-8")
            # Wait for the polling loop + debounce.
            await asyncio.sleep(2.5)
            # Registry should now contain the spec.
            specs = registry.for_event(EventType.PRE_TOOL_USE)
            assert any(s.hook_id == "test-1" for s in specs), (
                f"registry did not pick up new hook; "
                f"specs={[s.hook_id for s in specs]}"
            )
        finally:
            await watcher.stop()


class TestHotReloadFailOpen:
    @pytest.mark.asyncio
    async def test_malformed_json_does_not_crash_watcher(self, tmp_path: Path) -> None:
        from harness.hooks.hot_reload import start_hook_hot_reload
        from harness.hooks.registry import HookRegistry
        hooks_dir = tmp_path / ".harness" / "hooks"
        hooks_dir.mkdir(parents=True)
        registry = HookRegistry()
        watcher = await start_hook_hot_reload(
            registry, tmp_path, debounce_ms=50,
        )
        try:
            (hooks_dir / "bad.json").write_text("not json {", encoding="utf-8")
            await asyncio.sleep(1.5)
            # No crash, no specs registered.
            assert len(registry.for_event(__import__("harness").hooks.events.EventType.PRE_TOOL_USE)) == 0
        finally:
            await watcher.stop()
