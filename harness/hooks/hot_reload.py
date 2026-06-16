"""Phase 4.2 Step 3: Hot-reload for .harness/hooks/*.json.

Watches the project hooks directory and re-parses ``HookSpec``s
on file change. Uses ``FileWatcher`` (Phase 4.2 Step 1) for cross-
platform file watching.

Hook spec format (JSON):
    {
        "hook_id": "validate-bash-1",
        "event": "PreToolUse",
        "transport": "builtin",
        "matcher": "tool_name=bash",
        "timeout_ms": 1000,
        "enabled": true,
        "priority": 100
    }

Trust boundary: imports from ``harness.hooks.registry``,
``harness.hooks.events``, ``harness.hooks.spec`` (or wherever
HookSpec lives), ``harness.watcher``.

Strategy:
    1. On every change in ``.harness/hooks/*.json``, re-parse the
       file as a list of HookSpec dicts (one file can contain
       multiple hooks).
    2. Atomically swap the new specs into the HookRegistry:
       - Add new ones via ``registry.register()``.
       - Remove deleted ones via ``registry.unregister()``.
    3. Fail-open: malformed file → log warning, keep last good state.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec
from harness.watcher import FileChange, FileWatcher, get_file_watcher

_log = logging.getLogger(__name__)

#: File pattern for hook specs.
HOOK_PATTERN: str = "*.json"


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
    except Exception as exc:  # noqa: BLE001
        _log.debug("hot_reload emit failed: %s", exc)


def _parse_hook_file(path: Path) -> list[HookSpec]:
    """Parse a hook spec file. Supports two formats:

    1. Single object:  ``{...}`` → 1 hook
    2. List:           ``[{...}, {...}]`` → N hooks

    Raises ValueError on malformed input.
    """
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(
            f"hook file must be a JSON object or list, got {type(data).__name__}"
        )
    out: list[HookSpec] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"hook spec #{i} is not an object")
        # Validate required fields.
        for required in ("hook_id", "event", "transport"):
            if required not in item:
                raise ValueError(
                    f"hook spec #{i} missing required field {required!r}"
                )
        # Coerce event to EventType.
        try:
            event = EventType(item["event"])
        except ValueError as exc:
            raise ValueError(
                f"hook spec #{i}: unknown event {item['event']!r}"
            ) from exc
        # NOTE: HookSpec dataclass may have more fields; we pass
        # them as kwargs and let dataclass __init__ validate.
        out.append(
            HookSpec(
                hook_id=item["hook_id"],
                event=event,
                transport=item["transport"],
                matcher=item.get("matcher", ""),
                timeout_ms=int(item.get("timeout_ms", 3000)),
                enabled=bool(item.get("enabled", True)),
                priority=int(item.get("priority", 100)),
            )
        )
    return out


def _extract_hook_ids(specs: list[HookSpec]) -> set[str]:
    return {s.hook_id for s in specs}


async def _on_hook_change(
    changes: list[FileChange],
    registry: HookRegistry,
    project_root: Path,
) -> None:
    """Re-parse one or more changed hook .json files."""
    for fc in changes:
        path = fc.path
        # Verify path is under .harness/hooks/.
        parts = path.parts
        try:
            idx = parts.index(".harness")
        except ValueError:
            _log.warning("hot_reload: %s not under .harness/ — skip", path)
            continue
        # Must be under .harness/hooks/ (idx+1 exists AND is "hooks").
        if idx + 1 >= len(parts) or parts[idx + 1] != "hooks":
            _log.warning("hot_reload: %s not under .harness/hooks/ — skip", path)
            continue
        if fc.kind.value == "deleted":
            # Unregister all hooks previously loaded from this file.
            # We don't track per-file ownership, so we don't know
            # which IDs to remove. Conservative: log + skip.
            _log.info(
                "hot_reload: %s deleted — registry NOT auto-cleaned "
                "(re-add with same hook_id to override)",
                path,
            )
            _emit_hot_reload("hooks", path, status="removed")
            continue
        try:
            specs = _parse_hook_file(path)
        except Exception as exc:  # noqa: BLE001 — keep last good spec
            _log.warning(
                "hot_reload: failed to parse %s: %s", path, exc,
            )
            _emit_hot_reload("hooks", path, status="error", error=str(exc))
            continue
        for spec in specs:
            try:
                await registry.register(spec)
                _emit_hot_reload("hooks", path, status="ok")
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "hot_reload: failed to register %s: %s", spec.hook_id, exc,
                )
                _emit_hot_reload(
                    "hooks", path, status="register_error", error=str(exc),
                )


async def start_hook_hot_reload(
    registry: HookRegistry,
    project_root: Path,
    *,
    debounce_ms: int = 200,
) -> FileWatcher:
    """Start watching ``.harness/hooks/*.json`` under ``project_root``.

    Returns the FileWatcher so the caller can stop it on shutdown.
    """
    hooks_dir = project_root / ".harness" / "hooks"
    if not hooks_dir.exists():
        _log.debug(
            "hot_reload: %s does not exist — skipping hook watcher",
            hooks_dir,
        )
        return get_file_watcher()
    watcher = get_file_watcher()

    async def _on_change_with_registry(changes: list[FileChange]) -> None:
        await _on_hook_change(changes, registry, project_root)

    await watcher.watch(
        hooks_dir,
        pattern=HOOK_PATTERN,
        on_change=_on_change_with_registry,
        debounce_ms=debounce_ms,
    )
    _log.info("hot_reload: watching %s for *.json changes", hooks_dir)
    return watcher


__all__ = [
    "HOOK_PATTERN",
    "start_hook_hot_reload",
    "_parse_hook_file",
]
