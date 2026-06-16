"""Phase 4.2+ hot-reload for ``.harness/privacy/*.json``.

Watches the project privacy directory and atomically swaps the
:class:`~harness.privacy.zone_filter.PrivacyZoneFilter` rules on
file change. Uses :class:`~harness.watcher.FileWatcher` for cross-
platform file watching (Phase 4.2 Step 1).

File format (JSON):
    Single object with ``default_action`` + ``rules`` list::

        {
            "default_action": "block",
            "rules": [
                {"pattern": "private/**", "action": "block"},
                {"pattern": "*.env", "action": "redact"}
            ]
        }

    Or just a list of rules (uses ``PrivacyZoneFilter``'s existing
    ``default_action`` from Settings)::

        [
            {"pattern": "private/**", "action": "block"},
            {"pattern": "*.env", "action": "redact"}
        ]

Trust boundary: imports from ``harness.privacy.zone_config``,
``harness.privacy.zone_filter``, ``harness.watcher``. NO imports of
``harness.observability``, ``harness.hooks``, ``harness.server``.

Strategy:
    1. On every change in ``.harness/privacy/*.json``, re-parse the
       file into a list of :class:`ZoneRule`.
    2. Atomically swap the rules into the PrivacyZoneFilter via
       :meth:`PrivacyZoneFilter.set_rules`.
    3. Fail-open: malformed file → log warning, keep last good rules.

Why not delete handling? Privacy zones are *cumulative*: deleting a
file should not silently disable the filter. The previously loaded
rules stay in effect; the user must restart the server to revert to
Settings defaults. This is conservative and safe.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from harness.privacy.zone_config import ZoneRule
from harness.privacy.zone_filter import PrivacyZoneFilter
from harness.watcher import FileChange, FileWatcher, get_file_watcher

_log = logging.getLogger(__name__)

#: File pattern for privacy zone configs.
PRIVACY_PATTERN: str = "*.json"

#: Valid action values (mirrors ``ZoneAction`` literal).
_VALID_ACTIONS: frozenset[str] = frozenset({"block", "redact", "skip"})


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


def _parse_privacy_file(path: Path, default_action: str) -> list[ZoneRule]:
    """Parse a privacy config file into a list of :class:`ZoneRule`.

    Args:
        path:           Path to the JSON file.
        default_action: Fallback action for rules without explicit
                        ``action`` field. Must be one of ``block``,
                        ``redact``, ``skip``.

    Returns:
        Ordered list of :class:`ZoneRule`.

    Raises:
        ValueError: On malformed JSON, wrong types, invalid action,
                    or missing required fields.
    """
    if default_action not in _VALID_ACTIONS:
        raise ValueError(
            f"invalid default_action {default_action!r}; "
            f"must be one of {sorted(_VALID_ACTIONS)}"
        )

    text = path.read_text(encoding="utf-8")
    data = json.loads(text)

    # Normalize to list of {pattern, action} dicts.
    file_default_action: str | None = None
    if isinstance(data, dict):
        # Top-level dict: may contain "default_action" + "rules".
        if "default_action" in data:
            file_default_action = str(data["default_action"])
            if file_default_action not in _VALID_ACTIONS:
                raise ValueError(
                    f"file-level default_action {file_default_action!r} "
                    f"invalid; must be one of {sorted(_VALID_ACTIONS)}"
                )
        rules_data = data.get("rules", [])
        if not isinstance(rules_data, list):
            raise ValueError(
                f"'rules' must be a list, got {type(rules_data).__name__}"
            )
        items: list[dict[str, Any]] = rules_data
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(
            f"privacy file must be a JSON object or list, "
            f"got {type(data).__name__}"
        )

    effective_default = file_default_action or default_action
    rules: list[ZoneRule] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"rule #{i} is not an object")
        if "pattern" not in item:
            raise ValueError(f"rule #{i} missing required field 'pattern'")
        pattern = str(item["pattern"]).strip()
        if not pattern:
            raise ValueError(f"rule #{i} has empty pattern")
        action_str = str(item.get("action", effective_default)).strip()
        if action_str not in _VALID_ACTIONS:
            raise ValueError(
                f"rule #{i} has invalid action {action_str!r}; "
                f"must be one of {sorted(_VALID_ACTIONS)}"
            )
        rules.append(ZoneRule(pattern=pattern, action=action_str))  # type: ignore[arg-type]
    return rules


async def _on_privacy_change(
    changes: list[FileChange],
    filter_: PrivacyZoneFilter,
    default_action: str,
) -> None:
    """Re-parse one or more changed privacy .json files."""
    for fc in changes:
        path = fc.path
        # Verify path is under .harness/privacy/.
        parts = path.parts
        try:
            idx = parts.index(".harness")
        except ValueError:
            _log.warning("hot_reload: %s not under .harness/ — skip", path)
            continue
        if idx + 1 >= len(parts) or parts[idx + 1] != "privacy":
            _log.warning(
                "hot_reload: %s not under .harness/privacy/ — skip", path,
            )
            continue
        if fc.kind.value == "deleted":
            _log.info(
                "hot_reload: %s deleted — rules NOT auto-removed "
                "(restart server to revert to defaults)",
                path,
            )
            _emit_hot_reload("privacy", path, status="removed")
            continue
        try:
            new_rules = _parse_privacy_file(path, default_action)
        except Exception as exc:  # noqa: BLE001 — keep last good rules
            _log.warning(
                "hot_reload: failed to parse %s: %s", path, exc,
            )
            _emit_hot_reload("privacy", path, status="error", error=str(exc))
            continue
        # Atomic swap.
        filter_.set_rules(new_rules)
        _emit_hot_reload("privacy", path, status="ok")


async def start_privacy_hot_reload(
    filter_: PrivacyZoneFilter,
    project_root: Path,
    *,
    default_action: str = "block",
    debounce_ms: int = 200,
    poll_interval_s: float = 1.0,
) -> FileWatcher:
    """Start watching ``.harness/privacy/*.json`` under ``project_root``.

    On file change, atomically swaps new :class:`ZoneRule` list into
    the provided PrivacyZoneFilter via :meth:`PrivacyZoneFilter.set_rules`.

    Args:
        filter_:         The PrivacyZoneFilter to reconfigure on change.
                         Must be the same instance stored in
                         ``app.state.privacy_zones``.
        project_root:    Project root (where ``.harness/`` lives).
        default_action:  Fallback action for rules without explicit
                         ``action`` field. Should match the Settings
                         value used to build the initial filter.
        debounce_ms:     Event coalesce window.
        poll_interval_s: Polling fallback interval (used only if
                         watchfiles is not installed or fails).

    Returns:
        The FileWatcher singleton so the caller can stop it on shutdown.
    """
    privacy_dir = project_root / ".harness" / "privacy"
    if not privacy_dir.exists():
        _log.debug(
            "hot_reload: %s does not exist — skipping privacy watcher",
            privacy_dir,
        )
        return get_file_watcher()
    watcher = get_file_watcher()

    async def _on_change_with_filter(changes: list[FileChange]) -> None:
        await _on_privacy_change(changes, filter_, default_action)

    await watcher.watch(
        privacy_dir,
        pattern=PRIVACY_PATTERN,
        on_change=_on_change_with_filter,
        debounce_ms=debounce_ms,
        poll_interval_s=poll_interval_s,
    )
    _log.info("hot_reload: watching %s for *.json changes", privacy_dir)
    return watcher


__all__ = [
    "PRIVACY_PATTERN",
    "start_privacy_hot_reload",
    "_parse_privacy_file",
]
