"""Tests for :mod:`harness.server.agent.tool_offloader` (Phase 3 v1.3.1)."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.agents.scratchpad import NoteLevel
from harness.agents.scratchpad_store import ScratchpadStore
from harness.config import Settings
from harness.server.agent.tool_offloader import (
    TOOL_OFFLOAD_TAG,
    TOOL_TAG_PREFIX,
    ToolOffloader,
)


# === Fixtures ===

@pytest.fixture
def tmp_store(tmp_path: Path) -> ScratchpadStore:
    return ScratchpadStore(
        tmp_path / "agent-jobs.db",
        session_id="sess-offload",
        agent_id="solomon",
    )


@pytest.fixture
def default_settings() -> Settings:
    return Settings()


def _build_content(byte_size: int) -> str:
    """Return a string of approximately ``byte_size`` UTF-8 bytes."""
    chunk = "x" * 1024
    n = max(1, byte_size // 1024)
    return (chunk * n)[:byte_size]


# === should_offload ===

class TestShouldOffload:
    def test_under_threshold_returns_false(self, tmp_store: ScratchpadStore) -> None:
        settings = Settings(tool_offload_threshold_bytes=2048)
        off = ToolOffloader(tmp_store, settings)
        assert off.should_offload("hello world") is False

    def test_over_threshold_returns_true(self, tmp_store: ScratchpadStore) -> None:
        settings = Settings(tool_offload_threshold_bytes=1024)
        off = ToolOffloader(tmp_store, settings)
        assert off.should_offload(_build_content(4096)) is True

    def test_disabled_setting_returns_false(self, tmp_store: ScratchpadStore) -> None:
        settings = Settings(
            tool_offload_enabled=False, tool_offload_threshold_bytes=1024,
        )
        off = ToolOffloader(tmp_store, settings)
        assert off.should_offload(_build_content(4096)) is False

    def test_empty_content_returns_false(self, tmp_store: ScratchpadStore) -> None:
        settings = Settings(tool_offload_threshold_bytes=1024)
        off = ToolOffloader(tmp_store, settings)
        assert off.should_offload("") is False
        assert off.should_offload("not empty but tiny") is False


# === offload ===

class TestOffload:
    async def test_writes_to_l2_and_returns_note_id(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_max_ms=5000,
        )
        off = ToolOffloader(tmp_store, settings)
        content = _build_content(2048)
        note_id = await off.offload(
            content, tool_name="bash", session_id="sess-offload",
        )
        assert note_id is not None
        # Verify note was written with the right level and tags.
        notes = await tmp_store.read_notes(NoteLevel.L2)
        assert any(n.id == note_id for n in notes)
        target = next(n for n in notes if n.id == note_id)
        assert target.content == content
        assert TOOL_OFFLOAD_TAG in target.tags
        assert f"{TOOL_TAG_PREFIX}bash" in target.tags

    async def test_returns_none_when_under_threshold(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        settings = Settings(tool_offload_threshold_bytes=1024 * 1024)
        off = ToolOffloader(tmp_store, settings)
        note_id = await off.offload(
            "small content", tool_name="read_file",
            session_id="sess-offload",
        )
        assert note_id is None

    async def test_returns_none_when_disabled(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        settings = Settings(
            tool_offload_enabled=False, tool_offload_threshold_bytes=1024,
        )
        off = ToolOffloader(tmp_store, settings)
        note_id = await off.offload(
            _build_content(4096), tool_name="bash",
            session_id="sess-offload",
        )
        assert note_id is None

    async def test_returns_none_when_empty_content(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        settings = Settings(tool_offload_threshold_bytes=1024)
        off = ToolOffloader(tmp_store, settings)
        assert await off.offload(
            "", tool_name="bash", session_id="sess-offload",
        ) is None

    async def test_returns_none_on_store_error(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        """If write_note raises, offload returns None (fail-open)."""
        await tmp_store.init()
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_max_ms=5000,
        )
        off = ToolOffloader(tmp_store, settings)
        # Force write_note to raise.
        async def _raise(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated DB failure")
        tmp_store.write_note = _raise  # type: ignore[method-assign]
        note_id = await off.offload(
            _build_content(4096), tool_name="bash",
            session_id="sess-offload",
        )
        assert note_id is None


# === read ===

class TestRead:
    async def test_truncates_to_max_bytes(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_max_ms=5000,
        )
        off = ToolOffloader(tmp_store, settings)
        content = _build_content(2048)
        note_id = await off.offload(
            content, tool_name="bash", session_id="sess-offload",
        )
        assert note_id is not None
        truncated = await off.read(note_id, max_bytes=100)
        assert truncated is not None
        assert len(truncated) == 100
        assert truncated == content[:100]

    async def test_missing_note_returns_none(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        settings = Settings()
        off = ToolOffloader(tmp_store, settings)
        assert await off.read(99999) is None

    async def test_invalid_id_returns_none(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        settings = Settings()
        off = ToolOffloader(tmp_store, settings)
        assert await off.read(0) is None
        assert await off.read(-1) is None
        # Non-int input must be tolerated via guard.
        assert await off.read("not-an-int") is None  # type: ignore[arg-type]


# === build_stub ===

class TestBuildStub:
    def test_stub_includes_preview_lines_and_read_hint(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_preview_lines=3,
            tool_offload_preview_max_chars=600,
        )
        off = ToolOffloader(tmp_store, settings)
        content = "line1\nline2\nline3\nline4"
        stub = off.build_stub(
            content, note_id=42, tool_name="bash",
        )
        # Header
        assert "[Tool result offloaded:" in stub
        assert "id=42" in stub
        assert "tool=bash" in stub
        # Preview: first 3 lines.
        assert "line1" in stub
        assert "line2" in stub
        assert "line3" in stub
        assert "line4" not in stub
        # Read hint.
        assert "scratchpad_read_offloaded(id=42)" in stub
        assert "scratchpad_search_offloaded(query)" in stub

    def test_stub_strips_control_chars(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_preview_lines=3,
            tool_offload_preview_max_chars=600,
        )
        off = ToolOffloader(tmp_store, settings)
        # Embed \x01 and \x07 in the content; they must not appear
        # in the preview (the regex strips \x00-\x1F except \n\t).
        content = "safe\x01\x07 first\nsafe second"
        stub = off.build_stub(
            content, note_id=1, tool_name="bash",
        )
        assert "\x01" not in stub
        assert "\x07" not in stub
        # \n and \t are preserved.
        assert "safe" in stub
        assert "first" in stub

    def test_stub_caps_preview_at_max_chars(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_preview_lines=10,
            tool_offload_preview_max_chars=64,
        )
        off = ToolOffloader(tmp_store, settings)
        content = "x" * 500  # one very long line
        stub = off.build_stub(
            content, note_id=1, tool_name="bash",
        )
        # The build_stub caps the preview at ``max_chars - 1`` chars
        # and appends a single "…" character. So the preview is
        # 63 x's + "…" = 64 visible chars, not 64 x's.
        preview_match = "x" * 63 + "…"
        assert preview_match in stub
        # No line of 500 x's should appear.
        assert "x" * 200 not in stub


# === audit ===

class TestAudit:
    async def test_audit_record_emitted_when_enabled(
        self, tmp_store: ScratchpadStore, tmp_path: Path,
    ) -> None:
        await tmp_store.init()
        # Set up a mock audit that captures record() calls.
        audit = MagicMock()
        audit.enabled = True
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_max_ms=5000,
            scratchpad_audit_log=True,
        )
        off = ToolOffloader(tmp_store, settings, audit=audit)
        # 2048 bytes > threshold of 1024.
        note_id = await off.offload(
            _build_content(2048), tool_name="bash",
            session_id="sess-offload", tool_call_id="call_abc",
        )
        assert note_id is not None
        # Audit must have been called once with the right shape.
        assert audit.record.call_count == 1
        kwargs = audit.record.call_args.kwargs
        assert kwargs["event"] == "tool_offload"
        assert kwargs["session_id"] == "sess-offload"
        assert kwargs["note_id"] == note_id
        assert kwargs["tool_name"] == "bash"
        assert kwargs["tool_call_id"] == "call_abc"
        assert kwargs["original_bytes"] == 2048

    async def test_audit_skipped_when_setting_disabled(
        self, tmp_store: ScratchpadStore,
    ) -> None:
        await tmp_store.init()
        audit = MagicMock()
        settings = Settings(
            tool_offload_threshold_bytes=1024,
            tool_offload_max_ms=5000,
            scratchpad_audit_log=False,  # off
        )
        off = ToolOffloader(tmp_store, settings, audit=audit)
        await off.offload(
            _build_content(2048), tool_name="bash",
            session_id="sess-offload",
        )
        assert audit.record.call_count == 0
