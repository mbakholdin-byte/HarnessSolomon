"""Tests for the hmem memory adapter (Phase 1, Step 2).

The hmem adapter is the L1 (hierarchical) storage. It reads/writes
``Memory`` records from/to the hmem file format used by the
``mcp__hmem__*`` MCP server (prefix-based: P/L/T/E/D/M/S/N/H/R/O).

We use a **file-backed fake** hmem store for unit tests so we don't
need the MCP server running. The production path goes through the
real hmem client; both paths conform to the same ``HmemAdapter`` API.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from harness.memory.adapters.hmem import HmemAdapter
from harness.memory.schema import Memory


# === Fixtures ===

@pytest.fixture
def hmem_dir(tmp_path: Path) -> Path:
    """Empty tmp dir for hmem .hmem files."""
    d = tmp_path / "hmem-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def adapter(hmem_dir: Path) -> HmemAdapter:
    """HmemAdapter backed by the tmp hmem dir."""
    return HmemAdapter(memory_dir=hmem_dir, agent="solomon")


# === Construction ===

def test_hmem_adapter_creates_dir_if_missing(tmp_path: Path) -> None:
    """Constructor creates the memory dir if it doesn't exist."""
    target = tmp_path / "new" / "subdir"
    HmemAdapter(memory_dir=target, agent="solomon")
    assert target.exists()
    assert target.is_dir()


def test_hmem_adapter_with_default_dir() -> None:
    """Default dir is ~/hmem or env HMEM_DIR."""
    import os
    a = HmemAdapter(agent="solomon")
    assert a.memory_dir.exists()


# === Write → Read round-trip ===

def test_hmem_write_and_read_l1_entry(adapter: HmemAdapter) -> None:
    """Writing a Memory(layer=L1) and reading it back returns the same."""
    m = Memory(
        id="m-001",
        content="harness Phase 0 Web MVP is done",
        layer="L1",
        source="hmem",
        tags=["#harness"],
    )
    adapter.write(m)

    entries = adapter.read(prefix="D")  # D = Decision prefix
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "m-001"
    assert e.content == "harness Phase 0 Web MVP is done"
    assert e.layer == "L1"
    assert e.source == "hmem"


def test_hmem_write_picks_prefix_from_metadata(adapter: HmemAdapter) -> None:
    """The hmem prefix (P/L/D/...) comes from Memory.metadata['hmem_prefix']."""
    m = Memory(
        id="m-002",
        content="Lesson: 4 parallel subagent runs work for independent steps",
        layer="L1",
        source="hmem",
        metadata={"hmem_prefix": "L"},
    )
    adapter.write(m)
    # Read by L-prefix
    lessons = adapter.read(prefix="L")
    assert len(lessons) == 1
    assert lessons[0].id == "m-002"


def test_hmem_default_prefix_for_layer_l1_is_d(adapter: HmemAdapter) -> None:
    """For L1 layer without explicit prefix metadata, default to 'D' (Decision)."""
    m = Memory(content="x", layer="L1", source="hmem")
    adapter.write(m)
    decisions = adapter.read(prefix="D")
    assert any(e.content == "x" for e in decisions)


def test_hmem_read_filters_by_prefix(adapter: HmemAdapter) -> None:
    """read(prefix='X') returns ONLY entries with that prefix."""
    m_d = Memory(id="d-1", content="Decision A", layer="L1", source="hmem",
                 metadata={"hmem_prefix": "D"})
    m_l = Memory(id="l-1", content="Lesson B", layer="L1", source="hmem",
                 metadata={"hmem_prefix": "L"})
    m_e = Memory(id="e-1", content="Error C", layer="L1", source="hmem",
                 metadata={"hmem_prefix": "E"})
    for m in (m_d, m_l, m_e):
        adapter.write(m)

    # D prefix → 1 entry
    assert {e.id for e in adapter.read(prefix="D")} == {"d-1"}
    # L prefix → 1 entry
    assert {e.id for e in adapter.read(prefix="L")} == {"l-1"}
    # E prefix → 1 entry
    assert {e.id for e in adapter.read(prefix="E")} == {"e-1"}


def test_hmem_read_empty_returns_empty_list(adapter: HmemAdapter) -> None:
    """read() on an empty store returns []."""
    assert adapter.read(prefix="D") == []


# === Search ===

def test_hmem_search_substring(adapter: HmemAdapter) -> None:
    """search() returns entries whose content contains the query (case-insensitive)."""
    adapter.write(Memory(id="a", content="MiniMax max_tools is 16", layer="L1", source="hmem",
                         metadata={"hmem_prefix": "D"}))
    adapter.write(Memory(id="b", content="Lesson: parallel agents", layer="L1", source="hmem",
                         metadata={"hmem_prefix": "L"}))
    adapter.write(Memory(id="c", content="MiniMax API returns 200", layer="L1", source="hmem",
                         metadata={"hmem_prefix": "D"}))

    results = adapter.search("minimax")
    ids = {r.id for r in results}
    assert ids == {"a", "c"}

    results2 = adapter.search("PARALLEL")  # case-insensitive
    assert {r.id for r in results2} == {"b"}


def test_hmem_search_empty_query_returns_all(adapter: HmemAdapter) -> None:
    """search('') returns all entries."""
    adapter.write(Memory(id="x", content="a", layer="L1", source="hmem"))
    adapter.write(Memory(id="y", content="b", layer="L1", source="hmem"))
    results = adapter.search("")
    assert {r.id for r in results} == {"x", "y"}


# === Persistence ===

def test_hmem_persists_across_instances(hmem_dir: Path) -> None:
    """Writing through one adapter, reading through a fresh one works."""
    a1 = HmemAdapter(memory_dir=hmem_dir, agent="solomon")
    a1.write(Memory(id="persist-1", content="survives reload", layer="L1", source="hmem",
                    metadata={"hmem_prefix": "D"}))

    a2 = HmemAdapter(memory_dir=hmem_dir, agent="solomon")
    loaded = a2.read(prefix="D")
    assert any(e.id == "persist-1" for e in loaded)


def test_hmem_uses_jsonl_format(hmem_dir: Path) -> None:
    """The on-disk format is JSONL (one JSON object per line)."""
    a = HmemAdapter(memory_dir=hmem_dir, agent="solomon")
    a.write(Memory(id="f-1", content="format check", layer="L1", source="hmem",
                   metadata={"hmem_prefix": "D"}))
    # Find the .hmem file
    files = list(hmem_dir.glob("*.hmem"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["id"] == "f-1"
    assert obj["content"] == "format check"


# === Provenance recording ===

def test_hmem_write_appends_provenance(adapter: HmemAdapter) -> None:
    """Writing through the adapter records a hmem hop in provenance."""
    m = Memory(
        id="prov-1",
        content="originated elsewhere",
        layer="L1",
        source="hmem",
        provenance=[{"layer": "L2", "source": "mem0", "id": "upstream-1"}],
        metadata={"hmem_prefix": "D"},
    )
    adapter.write(m)

    loaded = adapter.read(prefix="D")
    target = next(e for e in loaded if e.id == "prov-1")
    assert len(target.provenance) == 2
    assert target.provenance[0].layer == "L2"
    assert target.provenance[0].id == "upstream-1"
    # The new hop was appended by the adapter
    assert target.provenance[-1].layer == "L1"
    assert target.provenance[-1].source == "hmem"
    assert target.provenance[-1].id == "prov-1"


# === Tags round-trip ===

def test_hmem_tags_round_trip(adapter: HmemAdapter) -> None:
    """Tags survive a write→read cycle."""
    m = Memory(
        id="t-1", content="x", layer="L1", source="hmem",
        tags=["#solomon", "#harness", "#phase-1"],
        metadata={"hmem_prefix": "D"},
    )
    adapter.write(m)
    loaded = adapter.read(prefix="D")
    target = next(e for e in loaded if e.id == "t-1")
    assert set(target.tags) == {"#solomon", "#harness", "#phase-1"}


# === Agent isolation ===

def test_hmem_per_agent_files(hmem_dir: Path) -> None:
    """Two agents on the same dir write to separate files."""
    a1 = HmemAdapter(memory_dir=hmem_dir, agent="alex")
    a2 = HmemAdapter(memory_dir=hmem_dir, agent="solomon")
    a1.write(Memory(id="x", content="alex entry", layer="L1", source="hmem"))
    a2.write(Memory(id="y", content="solomon entry", layer="L1", source="hmem"))

    # alex doesn't see solomon's
    alex_entries = a1.read(prefix="D")
    assert all(e.content != "solomon entry" for e in alex_entries)
    solomon_entries = a2.read(prefix="D")
    assert any(e.content == "solomon entry" for e in solomon_entries)
