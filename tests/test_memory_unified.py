"""Tests for the Unified Memory facade (Phase 1, Step 6).

The facade is the high-level entry point for the 4-layer memory
system. It owns one instance of each adapter (hmem, mem0, hybrid,
file) and exposes a single API:

  - ``write(memory)``        — dual-write to multiple layers
  - ``read(layer=X)``        — read from a specific layer
  - ``search(query)``        — search across all layers
  - ``recent(n)``            — recent entries (L3 hybrid)

Dual-write policy:
  - ``primary`` layer:    MUST be written
  - ``mirror`` layers:    ALSO written (best-effort; failures are
                          logged but do not abort the write)
  - The same Memory (by id) goes to every layer — provenance
    hops are appended as it travels.

By default, the primary is ``L2`` (mem0) and mirrors are ``L3`` and
``L4``. This is the Solomon canon: the semantic store is the
source of truth for retrieval; the episodic and file layers are
for search and human review.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness.memory.adapters.file import FileAdapter
from harness.memory.adapters.hmem import HmemAdapter
from harness.memory.adapters.hybrid import HybridAdapter
from harness.memory.adapters.mem0 import Mem0Adapter
from harness.memory.schema import Memory, MemoryLayer
from harness.memory.unified import (
    DEFAULT_DUAL_WRITE_POLICY,
    UnifiedMemory,
)


# === Fixtures ===

@pytest.fixture
def unified(tmp_path: Path) -> UnifiedMemory:
    """A UnifiedMemory backed by 4 fresh tmp dirs (one per adapter)."""
    return UnifiedMemory(
        hmem_dir=tmp_path / "hmem",
        mem0_dir=tmp_path / "mem0",
        hybrid_dir=tmp_path / "hybrid",
        file_dir=tmp_path / "file",
    )


@pytest.fixture
def primary_only(tmp_path: Path) -> UnifiedMemory:
    """A UnifiedMemory that writes to the primary layer only (no mirrors)."""
    return UnifiedMemory(
        hmem_dir=tmp_path / "hmem",
        mem0_dir=tmp_path / "mem0",
        hybrid_dir=tmp_path / "hybrid",
        file_dir=tmp_path / "file",
        dual_write_policy={"primary": "L2", "mirrors": []},
    )


# === Construction ===

def test_unified_creates_all_adapters(unified: UnifiedMemory) -> None:
    """All 4 adapters are constructed and reachable."""
    assert isinstance(unified.hmem, HmemAdapter)
    assert isinstance(unified.mem0, Mem0Adapter)
    assert isinstance(unified.hybrid, HybridAdapter)
    assert isinstance(unified.file, FileAdapter)


def test_unified_default_policy_is_canonic() -> None:
    """Default dual-write policy: primary=L2, mirrors=[L3, L4]."""
    assert DEFAULT_DUAL_WRITE_POLICY["primary"] == "L2"
    assert set(DEFAULT_DUAL_WRITE_POLICY["mirrors"]) == {"L3", "L4"}


def test_unified_invalid_layer_in_policy_raises(tmp_path: Path) -> None:
    """An invalid primary layer in the policy raises at construction."""
    with pytest.raises(ValueError):
        UnifiedMemory(
            hmem_dir=tmp_path / "hmem",
            mem0_dir=tmp_path / "mem0",
            hybrid_dir=tmp_path / "hybrid",
            file_dir=tmp_path / "file",
            dual_write_policy={"primary": "L9", "mirrors": []},
        )


# === Write — dual-write ===

def test_unified_write_dual_layer(
    unified: UnifiedMemory, tmp_path: Path
) -> None:
    """Default policy: writes to L2 (primary) + L3 + L4 (mirrors)."""
    m = Memory(
        id="u-1",
        content="User prefers concise answers",
        layer="L2",
        source="mem0",
    )
    unified.write(m)

    # L2 (primary) has it
    assert any(e.id == "u-1" for e in unified.mem0.read())
    # L3 (mirror) has it
    assert any(e.id == "u-1" for e in unified.hybrid.read())
    # L4 (mirror) has it
    assert unified.file.get("u-1") is not None
    # L1 is NOT written under the default policy
    l1_entries = unified.hmem.read()
    assert all(e.id != "u-1" for e in l1_entries)


def test_unified_write_l1_entry_uses_hmem(
    unified: UnifiedMemory, tmp_path: Path
) -> None:
    """An L1 entry is routed to the hmem adapter (L1 is its natural home)."""
    m = Memory(
        id="l1-1",
        content="harness Phase 0 is done",
        layer="L1",
        source="hmem",
    )
    unified.write(m)
    # L1 storage is the file fallback
    assert any(e.id == "l1-1" for e in unified.hmem.read())


def test_unified_write_primary_only(primary_only: UnifiedMemory) -> None:
    """With no mirrors, only the primary layer is touched."""
    m = Memory(
        id="po-1", content="x", layer="L2", source="mem0"
    )
    primary_only.write(m)
    # L2 has it
    assert any(e.id == "po-1" for e in primary_only.mem0.read())
    # L3 / L4 are empty
    assert primary_only.hybrid.read() == []
    assert primary_only.file.get("po-1") is None


def test_unified_write_records_provenance(
    unified: UnifiedMemory, tmp_path: Path
) -> None:
    """Each layer appends its own provenance hop; the same id travels through."""
    m = Memory(
        id="prov-u-1", content="x", layer="L2", source="mem0",
        provenance=[{"layer": "L1", "source": "hmem", "id": "upstream"}],
    )
    unified.write(m)

    # L2 has the L2/mem0 hop
    l2_target = next(e for e in unified.mem0.read() if e.id == "prov-u-1")
    layers_seen = [p.layer for p in l2_target.provenance]
    assert "L2" in layers_seen

    # L3 has the L3/hybrid hop
    l3_target = next(e for e in unified.hybrid.read() if e.id == "prov-u-1")
    layers_seen = [p.layer for p in l3_target.provenance]
    assert "L3" in layers_seen

    # L4 has the L4/file hop
    l4_target = unified.file.get("prov-u-1")
    assert l4_target is not None
    layers_seen = [p.layer for p in l4_target.provenance]
    assert "L4" in layers_seen


# === Mirror failure does not abort write ===

def test_unified_write_continues_if_mirror_fails(
    unified: UnifiedMemory,
) -> None:
    """If a mirror layer raises, the primary write still succeeds."""
    # Break the file adapter
    unified.file.write = MagicMock(side_effect=RuntimeError("disk full"))
    m = Memory(id="fail-1", content="x", layer="L2", source="mem0")
    unified.write(m)
    # Primary is intact
    assert any(e.id == "fail-1" for e in unified.mem0.read())


# === Read ===

def test_unified_read_returns_layer_subset(unified: UnifiedMemory) -> None:
    """read(layer=X) returns entries from the X adapter only."""
    unified.write(Memory(id="r-1", content="L2 entry", layer="L2", source="mem0"))
    unified.write(Memory(id="r-2", content="L1 entry", layer="L1", source="hmem"))
    unified.write(Memory(id="r-3", content="another L2", layer="L2", source="mem0"))

    l2 = unified.read(layer="L2")
    assert {e.id for e in l2} == {"r-1", "r-3"}
    l1 = unified.read(layer="L1")
    assert {e.id for e in l1} == {"r-2"}


def test_unified_read_all_layers(unified: UnifiedMemory) -> None:
    """read() with no layer returns everything from all 4 adapters (dedup by id)."""
    unified.write(Memory(id="all-1", content="L2", layer="L2", source="mem0"))
    unified.write(Memory(id="all-1", content="L3", layer="L3", source="hybrid"))  # same id
    unified.write(Memory(id="all-2", content="L1", layer="L1", source="hmem"))

    all_entries = unified.read()
    # Same id in 2 layers → 1 unique id
    by_id = {e.id for e in all_entries}
    assert by_id == {"all-1", "all-2"}


# === Search ===

def test_unified_search_routes_to_correct_adapter(
    unified: UnifiedMemory,
) -> None:
    """search() hits all 4 adapters and merges by id (with provenance hop info)."""
    unified.write(Memory(id="s-1", content="MiniMax M2.7", layer="L2", source="mem0"))
    unified.write(Memory(id="s-2", content="MiniMax streaming", layer="L3", source="hybrid"))
    unified.write(Memory(id="s-3", content="unrelated", layer="L2", source="mem0"))

    results = unified.search("minimax")
    # Deduplicated by id; each result is a Memory
    assert {r.id for r in results} == {"s-1", "s-2"}


def test_unified_search_empty_returns_all(unified: UnifiedMemory) -> None:
    """search('') returns everything across all layers (dedup by id)."""
    unified.write(Memory(id="e-1", content="a", layer="L2", source="mem0"))
    unified.write(Memory(id="e-2", content="b", layer="L3", source="hybrid"))
    results = unified.search("")
    assert {r.id for r in results} == {"e-1", "e-2"}


# === Recent ===

def test_unified_recent_returns_latest(unified: UnifiedMemory) -> None:
    """recent(n) is delegated to the hybrid adapter (L3 episodic)."""
    for i in range(5):
        unified.write(Memory(id=f"r-{i}", content=f"v{i}", layer="L3", source="hybrid"))
    last3 = unified.recent(n=3)
    assert [e.id for e in last3] == ["r-2", "r-3", "r-4"]


# === Delete ===

def test_unified_delete_removes_from_all_layers(
    unified: UnifiedMemory,
) -> None:
    """delete() removes the entry from every layer that has it."""
    unified.write(Memory(id="del-u-1", content="x", layer="L2", source="mem0"))
    # Confirmed it landed in 3 layers
    assert unified.mem0.read()  # populated
    assert unified.hybrid.read()  # populated
    assert unified.file.get("del-u-1") is not None

    # Delete
    n_removed = unified.delete("del-u-1")
    assert n_removed == 3  # mem0 + hybrid + file
    # All gone
    assert all(e.id != "del-u-1" for e in unified.mem0.read())
    assert all(e.id != "del-u-1" for e in unified.hybrid.read())
    assert unified.file.get("del-u-1") is None
