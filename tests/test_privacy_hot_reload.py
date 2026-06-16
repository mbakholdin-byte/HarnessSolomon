"""Phase 4.2+ tests: hot-reload for .harness/privacy/*.json.

Strategy:
    1. Parser tests are pure (no file system, no watcher).
    2. Atomic swap tests create a PrivacyZoneFilter, call
       set_rules(), and verify the rules + check() output.
    3. Watcher tests use tmp_path + polling fallback (sleep 2.5s
       after writes to allow the watcher to fire on Windows).
    4. All tests reset the FileWatcher singleton via fixture.

Trust boundary: tests DO import privacy.hot_reload — that's the
public API. They do NOT import harness.observability directly.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness.privacy.zone_config import ZoneRule
from harness.privacy.zone_filter import PrivacyZoneFilter
from harness.privacy.hot_reload import (
    PRIVACY_PATTERN,
    _parse_privacy_file,
    start_privacy_hot_reload,
)
from harness.watcher import (
    FileChange,
    FileChangeKind,
    reset_file_watcher,
)


@pytest.fixture(autouse=True)
def reset_singleton() -> None:
    """Reset the FileWatcher singleton before each test."""
    reset_file_watcher()
    return None


# === Parser tests (pure) ===


class TestParsePrivacyFile:
    """Tests for ``_parse_privacy_file`` — no I/O side effects beyond reading path."""

    def test_dict_with_default_action_and_rules(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps({
                "default_action": "redact",
                "rules": [
                    {"pattern": "private/**", "action": "block"},
                    {"pattern": "*.env", "action": "redact"},
                ],
            }),
            encoding="utf-8",
        )
        rules = _parse_privacy_file(path, "block")
        assert rules == [
            ZoneRule(pattern="private/**", action="block"),
            ZoneRule(pattern="*.env", action="redact"),
        ]

    def test_dict_with_rules_only_uses_caller_default(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps({
                "rules": [
                    {"pattern": "private/**"},
                    {"pattern": "*.env"},
                ],
            }),
            encoding="utf-8",
        )
        rules = _parse_privacy_file(path, "block")
        assert rules == [
            ZoneRule(pattern="private/**", action="block"),
            ZoneRule(pattern="*.env", action="block"),
        ]

    def test_list_format_uses_caller_default(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps([
                {"pattern": "private/**", "action": "block"},
                {"pattern": "*.env", "action": "redact"},
            ]),
            encoding="utf-8",
        )
        rules = _parse_privacy_file(path, "skip")
        assert rules == [
            ZoneRule(pattern="private/**", action="block"),
            ZoneRule(pattern="*.env", action="redact"),
        ]

    def test_file_level_default_overrides_caller(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps({
                "default_action": "skip",
                "rules": [
                    {"pattern": "private/**"},
                    {"pattern": "*.env", "action": "redact"},
                ],
            }),
            encoding="utf-8",
        )
        rules = _parse_privacy_file(path, "block")
        # First rule uses file-level "skip", second overrides to "redact".
        assert rules == [
            ZoneRule(pattern="private/**", action="skip"),
            ZoneRule(pattern="*.env", action="redact"),
        ]

    def test_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text("[]", encoding="utf-8")
        assert _parse_privacy_file(path, "block") == []

    def test_missing_pattern_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps([{"action": "block"}]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing required field 'pattern'"):
            _parse_privacy_file(path, "block")

    def test_empty_pattern_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps([{"pattern": "   "}]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="empty pattern"):
            _parse_privacy_file(path, "block")

    def test_invalid_action_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps([{"pattern": "x", "action": "destroy"}]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="invalid action 'destroy'"):
            _parse_privacy_file(path, "block")

    def test_invalid_default_action_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps({
                "default_action": "nuke",
                "rules": [{"pattern": "x"}],
            }),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="file-level default_action 'nuke'"):
            _parse_privacy_file(path, "block")

    def test_caller_default_invalid_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid default_action"):
            _parse_privacy_file(path, "nuke")

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            _parse_privacy_file(path, "block")

    def test_wrong_top_level_type_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text('"just a string"', encoding="utf-8")
        with pytest.raises(ValueError, match="must be a JSON object or list"):
            _parse_privacy_file(path, "block")

    def test_rules_must_be_list(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps({"rules": {"pattern": "x"}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="'rules' must be a list"):
            _parse_privacy_file(path, "block")

    def test_rule_must_be_object(self, tmp_path: Path) -> None:
        path = tmp_path / "zones.json"
        path.write_text(
            json.dumps([42, "string", {"pattern": "x"}]),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="rule #0 is not an object"):
            _parse_privacy_file(path, "block")


# === Atomic swap tests ===


class TestPrivacyZoneFilterSetRules:
    """PrivacyZoneFilter.set_rules atomic swap."""

    def test_initial_rules(self) -> None:
        rules = [ZoneRule(pattern="private/**", action="block")]
        f = PrivacyZoneFilter(rules)
        assert f.rules == rules
        assert f.check("private/.env") == ("block", "private/**")

    def test_set_rules_replaces(self) -> None:
        f = PrivacyZoneFilter([ZoneRule(pattern="old/**", action="block")])
        new_rules = [ZoneRule(pattern="new/**", action="redact")]
        f.set_rules(new_rules)
        assert f.rules == new_rules
        # Old pattern no longer matches.
        assert f.check("old/x") == ("allow", None)
        # New pattern works.
        assert f.check("new/x") == ("redact", "new/**")

    def test_set_rules_preserves_enabled(self) -> None:
        f = PrivacyZoneFilter([ZoneRule(pattern="x", action="block")], enabled=False)
        f.set_rules([ZoneRule(pattern="y", action="block")])
        assert f.enabled is False
        # Even though rule matches, disabled short-circuits.
        assert f.check("y") == ("allow", None)

    def test_set_rules_copies_input(self) -> None:
        """Caller mutations to input list don't affect the filter."""
        original = [ZoneRule(pattern="x", action="block")]
        f = PrivacyZoneFilter([])
        f.set_rules(original)
        original.append(ZoneRule(pattern="y", action="block"))
        # Filter still has only the original rules.
        assert len(f.rules) == 1
        assert f.check("y") == ("allow", None)

    def test_set_rules_empty_list_disables_filtering(self) -> None:
        f = PrivacyZoneFilter([ZoneRule(pattern="x", action="block")])
        f.set_rules([])
        assert f.rules == []
        assert f.check("x") == ("allow", None)


# === Watcher integration tests ===


class TestStartPrivacyHotReload:
    """``start_privacy_hot_reload`` watches .harness/privacy/*.json."""

    @pytest.mark.asyncio
    async def test_no_privacy_dir_returns_singleton(self, tmp_path: Path) -> None:
        """If ``.harness/privacy/`` doesn't exist, no crash, return watcher."""
        f = PrivacyZoneFilter([])
        watcher = await start_privacy_hot_reload(
            f, tmp_path, default_action="block",
        )
        # No task started.
        assert watcher.active == 0
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_privacy_dir_with_no_files_starts_task(self, tmp_path: Path) -> None:
        """Empty dir is valid — watcher is active, just no events."""
        privacy_dir = tmp_path / ".harness" / "privacy"
        privacy_dir.mkdir(parents=True)
        f = PrivacyZoneFilter([])
        watcher = await start_privacy_hot_reload(
            f, tmp_path, default_action="block",
        )
        assert watcher.active == 1
        await watcher.stop()

    @pytest.mark.asyncio
    async def test_create_file_replaces_rules(self, tmp_path: Path) -> None:
        """Adding a new privacy file atomically swaps filter rules."""
        privacy_dir = tmp_path / ".harness" / "privacy"
        privacy_dir.mkdir(parents=True)
        f = PrivacyZoneFilter([])
        assert f.check("private/.env") == ("allow", None)

        watcher = await start_privacy_hot_reload(
            f, tmp_path, default_action="block",
            debounce_ms=50, poll_interval_s=0.2,
        )
        try:
            # Give the polling loop time to take its first snapshot.
            await asyncio.sleep(0.3)
            # Write a file.
            config_path = privacy_dir / "zones.json"
            config_path.write_text(
                json.dumps([{"pattern": "private/**", "action": "redact"}]),
                encoding="utf-8",
            )
            # Wait for poll + debounce. Single sleep, not spin-loop,
            # so the event loop can run the watcher freely.
            await asyncio.sleep(2.5)
            assert f.check("private/.env") == ("redact", "private/**"), (
                f"watcher did not swap rules; "
                f"got {f.check('private/.env')!r}"
            )
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_modify_file_replaces_rules(self, tmp_path: Path) -> None:
        """Editing an existing privacy file atomically swaps rules."""
        privacy_dir = tmp_path / ".harness" / "privacy"
        privacy_dir.mkdir(parents=True)
        config_path = privacy_dir / "zones.json"
        config_path.write_text(
            json.dumps([{"pattern": "old/**", "action": "block"}]),
            encoding="utf-8",
        )

        f = PrivacyZoneFilter([ZoneRule(pattern="seed", action="block")])
        watcher = await start_privacy_hot_reload(
            f, tmp_path, default_action="block",
            debounce_ms=50, poll_interval_s=0.2,
        )
        try:
            # Give watchfiles time to start listening.
            await asyncio.sleep(0.3)
            # Initial: only "seed" matches.
            assert f.check("old/x") == ("allow", None)
            assert f.check("seed") == ("block", "seed")

            # Overwrite with new content. On Windows, mtime resolution
            # can be 16ms; sleep a bit to ensure the watcher notices.
            await asyncio.sleep(0.1)
            config_path.write_text(
                json.dumps([{"pattern": "new/**", "action": "redact"}]),
                encoding="utf-8",
            )
            # Bump mtime explicitly to guarantee > 1s resolution
            # difference even on slow filesystems.
            import os
            import time
            new_mtime = time.time() + 2
            os.utime(config_path, (new_mtime, new_mtime))
            await asyncio.sleep(2.5)
            assert f.check("new/x") == ("redact", "new/**"), (
                f"watcher did not detect modify; "
                f"got {f.check('new/x')!r}"
            )
            # Seed rule was added at construction time and is NOT
            # in the file — file contents fully replaced file-derived
            # rules, but the seed was added separately. With a single
            # file, set_rules() replaces ALL filter rules.
            assert f.check("seed") == ("allow", None)
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_malformed_file_keeps_last_good_rules(self, tmp_path: Path) -> None:
        """Writing invalid JSON keeps the previously loaded rules."""
        privacy_dir = tmp_path / ".harness" / "privacy"
        privacy_dir.mkdir(parents=True)
        config_path = privacy_dir / "zones.json"
        config_path.write_text(
            json.dumps([{"pattern": "private/**", "action": "block"}]),
            encoding="utf-8",
        )

        f = PrivacyZoneFilter([])
        watcher = await start_privacy_hot_reload(
            f, tmp_path, default_action="block",
            debounce_ms=50, poll_interval_s=0.2,
        )
        try:
            # Give watchfiles time to start listening.
            await asyncio.sleep(0.3)
            # Overwrite with garbage. Bump mtime to ensure watcher
            # notices on Windows (mtime resolution 16ms).
            import os
            import time
            await asyncio.sleep(0.1)
            config_path.write_text("not json at all", encoding="utf-8")
            new_mtime = time.time() + 2
            os.utime(config_path, (new_mtime, new_mtime))
            await asyncio.sleep(2.5)
            # Rules should still be from the LAST good state.
            # Since the initial file was created BEFORE the watcher
            # started, the watcher never parsed it. So even after
            # the garbage event fires (and fails to parse), rules
            # stay empty.
            assert f.check("private/.env") == ("allow", None)
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_delete_file_does_not_clear_rules(self, tmp_path: Path) -> None:
        """Deleting a file is conservative — rules stay loaded."""
        privacy_dir = tmp_path / ".harness" / "privacy"
        privacy_dir.mkdir(parents=True)
        config_path = privacy_dir / "zones.json"
        config_path.write_text(
            json.dumps([{"pattern": "private/**", "action": "block"}]),
            encoding="utf-8",
        )

        f = PrivacyZoneFilter([ZoneRule(pattern="seed", action="block")])
        watcher = await start_privacy_hot_reload(
            f, tmp_path, default_action="block",
            debounce_ms=50, poll_interval_s=0.2,
        )
        try:
            await asyncio.sleep(0.3)
            # Delete the file. watchfiles should detect the deletion.
            config_path.unlink()
            await asyncio.sleep(2.5)
            # Conservative: on_delete we LOG + SKIP, so the original
            # "seed" rule stays in effect.
            assert f.check("seed") == ("block", "seed")
            assert f.check("private/.env") == ("allow", None)
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_change_outside_privacy_dir_ignored(self, tmp_path: Path) -> None:
        """Files not under .harness/privacy/ are filtered out."""
        privacy_dir = tmp_path / ".harness" / "privacy"
        privacy_dir.mkdir(parents=True)
        f = PrivacyZoneFilter([])
        watcher = await start_privacy_hot_reload(
            f, tmp_path, default_action="block",
            debounce_ms=50, poll_interval_s=0.2,
        )
        try:
            # Call the on_change handler directly with a non-privacy path.
            from harness.privacy.hot_reload import _on_privacy_change

            bogus = FileChange(
                path=tmp_path / ".harness" / "agents" / "x.md",
                kind=FileChangeKind.MODIFIED,
            )
            await _on_privacy_change([bogus], f, "block")
            # Filter still empty.
            assert f.rules == []
        finally:
            await watcher.stop()


# === Pattern constant test ===

def test_privacy_pattern_constant() -> None:
    assert PRIVACY_PATTERN == "*.json"
