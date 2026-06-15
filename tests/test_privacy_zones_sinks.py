"""Phase 3 v1.5.0 Step 3: integration tests for Tier 1 privacy zone sinks.

Verifies that ``ToolRuntime`` enforces the path-based privacy filter
on the three Tier 1 sinks:
- ``read_file`` (file read)
- ``grep`` (content search, gated by path)
- ``glob`` (file enumeration, gated by path)

Tier 2/3 sinks (scratchpad write, embeddings, webhooks) are deferred
to v1.6.0+ and are NOT covered here.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from harness.privacy import PrivacyZoneFilter, ZoneRule, parse_zones
from harness.server.agent.runtime import ToolRuntime


# --- Fixtures ---


@pytest.fixture
def project_with_sensitive(tmp_path: Path) -> Path:
    """Create a project tree with sensitive + non-sensitive files.

    Layout:
        tmp_path/
        ├── private/
        │   └── .env         (matches default 'private/**' pattern)
        ├── config/
        │   └── .env         (matches '*.env' basename)
        ├── src/
        │   └── main.py      (allowed)
        ├── secrets/
        │   └── api.key      (matches 'secrets/**')
        └── .ssh/
            └── id_rsa       (matches '.ssh/**')
    """
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / ".env").write_text("SECRET=12345", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / ".env").write_text("DB_PASS=hunter2", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text('print("hello")', encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "api.key").write_text("AKIA-EXAMPLE", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("-----BEGIN RSA-----", encoding="utf-8")
    return tmp_path


@pytest.fixture
def default_filter() -> PrivacyZoneFilter:
    """PrivacyZoneFilter with default patterns (block action)."""
    rules = parse_zones("", "", "block")
    return PrivacyZoneFilter(rules)


# --- read_file sink ---


class TestReadFileSink:
    """read_file respects privacy zone decisions."""

    @pytest.mark.asyncio
    async def test_block_returns_error_result(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """Block action → ok=False, error mentions path and pattern."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("read_file", {"path": "private/.env"})
        assert result.ok is False
        assert "private/.env" in (result.error or "")
        assert "private/**" in (result.error or "")

    @pytest.mark.asyncio
    async def test_block_prevents_file_read(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """Block fires BEFORE the file is read (no leak via error message)."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("read_file", {"path": "private/.env"})
        # The error must NOT contain the file content.
        assert "SECRET=12345" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_basename_fallback_block(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """config/.env blocked via '*.env' basename pattern."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("read_file", {"path": "config/.env"})
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_allow_path(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """Non-sensitive path is allowed."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("read_file", {"path": "src/main.py"})
        assert result.ok is True
        assert "print(\"hello\")" in (result.output or "")

    @pytest.mark.asyncio
    async def test_no_filter_backward_compat(
        self, project_with_sensitive: Path,
    ) -> None:
        """privacy_zones=None (default) → no filtering."""
        rt = ToolRuntime(project_root=project_with_sensitive)  # no filter
        result = await rt.execute("read_file", {"path": "private/.env"})
        assert result.ok is True
        assert "SECRET=12345" in (result.output or "")


# --- redact / skip actions ---


class TestRedactAndSkipActions:
    """redact/skip actions differ from block."""

    @pytest.mark.asyncio
    async def test_redact_returns_placeholder(
        self, project_with_sensitive: Path,
    ) -> None:
        """Redact action → ok=True with [PRIVATE: ...] placeholder."""
        rules = [ZoneRule(pattern="secrets/**", action="redact")]
        filt = PrivacyZoneFilter(rules)
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=filt)
        result = await rt.execute("read_file", {"path": "secrets/api.key"})
        assert result.ok is True
        assert "[PRIVATE" in (result.output or "")
        assert "secrets/**" in (result.output or "")
        # Original content NOT exposed.
        assert "AKIA-EXAMPLE" not in (result.output or "")

    @pytest.mark.asyncio
    async def test_skip_returns_empty(
        self, project_with_sensitive: Path,
    ) -> None:
        """Skip action → ok=True with empty output."""
        rules = [ZoneRule(pattern="secrets/**", action="skip")]
        filt = PrivacyZoneFilter(rules)
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=filt)
        result = await rt.execute("read_file", {"path": "secrets/api.key"})
        assert result.ok is True
        assert result.output == ""


# --- grep sink ---


class TestGrepSink:
    """grep respects privacy zone decisions on the search root."""

    @pytest.mark.asyncio
    async def test_grep_blocked_on_private_dir(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """grep rooted in private/ is blocked (would expose private contents)."""
        if shutil.which("rg") is None:
            pytest.skip("rg (ripgrep) not installed")
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute(
            "grep", {"pattern": "SECRET", "path": "private"},
        )
        assert result.ok is False
        assert "private" in (result.error or "")

    @pytest.mark.asyncio
    async def test_grep_allowed_on_src(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """grep rooted in src/ works normally."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("grep", {"pattern": "print", "path": "src"})
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_grep_no_path_uses_root(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """grep with no path → uses project_root, no privacy check on path arg."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("grep", {"pattern": "print"})  # no path
        assert result.ok is True


# --- glob sink ---


class TestGlobSink:
    """glob respects privacy zone decisions on the search root."""

    @pytest.mark.asyncio
    async def test_glob_blocked_on_secrets(
        self, project_with_sensitive: Path,
    ) -> None:
        """glob rooted in secrets/ is blocked (would enumerate the dir).

        Uses a custom filter with both ``secrets`` (root, no slash) and
        ``secrets/**`` (nested) patterns — the first matches the
        search root itself before glob starts enumerating.
        """
        rules = [
            ZoneRule(pattern="secrets", action="block"),
            ZoneRule(pattern="secrets/**", action="block"),
        ]
        filt = PrivacyZoneFilter(rules)
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=filt)
        result = await rt.execute("glob", {"pattern": "**/*", "path": "secrets"})
        assert result.ok is False
        assert "secrets" in (result.error or "")

    @pytest.mark.asyncio
    async def test_glob_allowed_on_src(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """glob rooted in src/ works normally."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("glob", {"pattern": "**/*.py", "path": "src"})
        assert result.ok is True
        assert "main.py" in (result.output or "")

    @pytest.mark.asyncio
    async def test_glob_no_path_uses_root(
        self, project_with_sensitive: Path, default_filter: PrivacyZoneFilter,
    ) -> None:
        """glob with no path → uses project_root, no privacy check on path arg."""
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=default_filter)
        result = await rt.execute("glob", {"pattern": "**/*.py"})
        assert result.ok is True


# --- Trust boundary + filter disabled ---


class TestDisabledAndFailsafe:
    """Privacy filter disabled or failsafe behaviour."""

    @pytest.mark.asyncio
    async def test_disabled_filter_allows_all(
        self, project_with_sensitive: Path,
    ) -> None:
        """enabled=False → filter is no-op, even for sensitive files."""
        rules = parse_zones("", "", "block")
        filt = PrivacyZoneFilter(rules, enabled=False)
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=filt)
        result = await rt.execute("read_file", {"path": "private/.env"})
        assert result.ok is True
        assert "SECRET=12345" in (result.output or "")

    @pytest.mark.asyncio
    async def test_audit_emitted_on_block(
        self, project_with_sensitive: Path,
    ) -> None:
        """Filter audit sink receives privacy_zone_blocked event."""
        # Build a minimal audit sink.
        captured: list[tuple[str, dict[str, Any]]] = []

        class _Audit:
            def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
                captured.append((event, payload or {}))

        rules = [ZoneRule(pattern="private/**", action="block")]
        filt = PrivacyZoneFilter(rules, audit=_Audit())
        rt = ToolRuntime(project_root=project_with_sensitive, privacy_zones=filt)
        await rt.execute("read_file", {"path": "private/.env"})
        assert len(captured) == 1
        event, payload = captured[0]
        assert event == "privacy_zone_blocked"
        assert payload["path"] == "private/.env"
        assert payload["pattern"] == "private/**"
