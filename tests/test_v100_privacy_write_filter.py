"""v1.0.0 write-time PrivacyZoneFilter tests — Phase 5.5 fix per Марк review.

Roadmap promises privacy zones as write-time filter (sensitive files
should NOT enter auto-memory). v1.0.0 fix: implement write-time
filter in ``UnifiedMemory.write`` via the injected ``privacy_zones``
parameter. When a memory's ``source_path`` matches a zone with
``action="block"``, the write is dropped before any adapter receives it.

Trust boundary: stdlib + harness.memory only. NO imports of harness.agents.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from harness.memory.schema import Memory
from harness.memory.unified import UnifiedMemory


@pytest.fixture
def tmp_memory_dirs() -> tuple[Path, Path, Path, Path]:
    """Provide four tmp dirs for the four memory adapters."""
    base = Path(tempfile.mkdtemp(prefix="harness_v100_privacy_"))
    return (
        base / "hmem",
        base / "mem0",
        base / "hybrid",
        base / "file",
    )


def _make_fake_privacy_zones(patterns: list[tuple[str, str]]) -> Any:
    """Build a minimal stand-in for PrivacyZoneFilter.

    ``patterns`` is a list of ``(glob, action)``. The resulting object
    exposes ``match(path) -> SimpleNamespace(pattern=..., action=...)``
    or ``None``.
    """
    import fnmatch
    from types import SimpleNamespace

    class FakeZone:
        def __init__(self, glob: str, action: str) -> None:
            self.pattern = glob
            self.action = action

    class FakeFilter:
        def __init__(self, patterns: list[tuple[str, str]]) -> None:
            self._patterns = [FakeZone(g, a) for g, a in patterns]

        def match(self, path: str) -> Any:
            for p in self._patterns:
                if fnmatch.fnmatch(path, p.pattern):
                    return SimpleNamespace(pattern=p.pattern, action=p.action)
            return None

    return FakeFilter(patterns)


def _make_memory(layer: str, source_path: str = "") -> Memory:
    return Memory(
        layer=layer,
        source="manual",
        content="test content",
        metadata={"source_path": source_path} if source_path else {},
        tags=["test"],
    )


@pytest.mark.asyncio
async def test_write_with_block_zone_drops_memory(tmp_memory_dirs: tuple[Path, Path, Path, Path]) -> None:
    """Write-time filter blocks sensitive files from entering any adapter."""
    hmem_dir, mem0_dir, hybrid_dir, file_dir = tmp_memory_dirs
    privacy_zones = _make_fake_privacy_zones([
        ("private/*", "block"),
        ("secrets/**", "block"),
    ])
    um = UnifiedMemory(
        hmem_dir=hmem_dir,
        mem0_dir=mem0_dir,
        hybrid_dir=hybrid_dir,
        file_dir=file_dir,
        dual_write_policy={"primary": "L3", "mirrors": ["L2"]},
        privacy_zones=privacy_zones,
    )

    # Try to write a sensitive memory — should be dropped.
    sensitive = _make_memory("L3", source_path="private/keys.txt")
    um.write(sensitive)

    # Verify NO adapter received it (search returns nothing).
    hits = um.search("keys.txt")
    assert hits == [], (
        f"sensitive memory should be blocked, but search returned: {hits}"
    )


@pytest.mark.asyncio
async def test_write_with_no_privacy_zones_proceeds(tmp_memory_dirs: tuple[Path, Path, Path, Path]) -> None:
    """Without privacy_zones injected, writes proceed (backward compat)."""
    hmem_dir, mem0_dir, hybrid_dir, file_dir = tmp_memory_dirs
    um = UnifiedMemory(
        hmem_dir=hmem_dir,
        mem0_dir=mem0_dir,
        hybrid_dir=hybrid_dir,
        file_dir=file_dir,
        dual_write_policy={"primary": "L3", "mirrors": ["L2"]},
        privacy_zones=None,
    )

    memory = _make_memory("L3", source_path="private/keys.txt")
    um.write(memory)

    hits = um.search("test content")
    assert len(hits) >= 1, "non-filtered memory should be searchable"


@pytest.mark.asyncio
async def test_write_with_non_block_zone_action_proceeds(tmp_memory_dirs: tuple[Path, Path, Path, Path]) -> None:
    """Zone with action='redact' or 'skip' (not 'block') does not drop write."""
    hmem_dir, mem0_dir, hybrid_dir, file_dir = tmp_memory_dirs
    privacy_zones = _make_fake_privacy_zones([
        ("private/*", "redact"),
    ])
    um = UnifiedMemory(
        hmem_dir=hmem_dir,
        mem0_dir=mem0_dir,
        hybrid_dir=hybrid_dir,
        file_dir=file_dir,
        dual_write_policy={"primary": "L3", "mirrors": ["L2"]},
        privacy_zones=privacy_zones,
    )

    memory = _make_memory("L3", source_path="private/notes.txt")
    um.write(memory)

    hits = um.search("test content")
    assert len(hits) >= 1, (
        "redact action should not drop writes (only 'block' does)"
    )


@pytest.mark.asyncio
async def test_write_with_unmatched_path_proceeds(tmp_memory_dirs: tuple[Path, Path, Path, Path]) -> None:
    """Source path not matching any zone → write proceeds normally."""
    hmem_dir, mem0_dir, hybrid_dir, file_dir = tmp_memory_dirs
    privacy_zones = _make_fake_privacy_zones([
        ("private/*", "block"),
    ])
    um = UnifiedMemory(
        hmem_dir=hmem_dir,
        mem0_dir=mem0_dir,
        hybrid_dir=hybrid_dir,
        file_dir=file_dir,
        dual_write_policy={"primary": "L3", "mirrors": ["L2"]},
        privacy_zones=privacy_zones,
    )

    memory = _make_memory("L3", source_path="public/readme.md")
    um.write(memory)

    hits = um.search("test content")
    assert len(hits) >= 1
