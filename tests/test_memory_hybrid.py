"""Tests for the fas-hybrid-memory adapter (Phase 1, Step 4).

The hybrid adapter is the L3 (episodic / artifact) storage. It
stores ``Memory`` records as episodes, scoped by ``project`` and
``tags`` (Solomon canon: ``project="FAS"``, ``tags=["#solomon"]``).

Production backend is fas-hybrid-memory (Qdrant + SQLite +
OpenSearch). This adapter provides a SQLite-only fallback that
conforms to the same API. The schema is forward-compatible: a real
Qdrant/OpenSearch adapter can be added without changing the
``HybridAdapter`` interface.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.memory.adapters.hybrid import HybridAdapter
from harness.memory.schema import Memory


# === Fixtures ===

@pytest.fixture
def hybrid_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hybrid-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def adapter(hybrid_dir: Path) -> HybridAdapter:
    return HybridAdapter(
        storage_dir=hybrid_dir,
        project="FAS",
        default_tags=["#solomon"],
    )


# === Construction ===

def test_hybrid_creates_dir(tmp_path: Path) -> None:
    """Constructor creates the storage dir if missing."""
    target = tmp_path / "fresh"
    HybridAdapter(storage_dir=target)
    assert target.exists()


def test_hybrid_default_storage_dir() -> None:
    """Default storage dir exists and is writable."""
    a = HybridAdapter()
    assert a.storage_dir.exists()
    assert a.storage_dir.is_dir()


def test_hybrid_default_project_and_tags() -> None:
    """Default project = 'FAS' (Solomon canon), default tag = '#solomon'."""
    a = HybridAdapter()
    assert a.project == "FAS"
    assert "#solomon" in a.default_tags


# === Write → Read ===

def test_hybrid_write_and_read(adapter: HybridAdapter) -> None:
    """Round-trip: write a Memory, read it back."""
    m = Memory(
        id="ep-1",
        content="Phase 0 Web MVP shipped 2026-06-14",
        layer="L3",
        source="hybrid",
        tags=["#milestone"],
    )
    adapter.write(m)
    entries = adapter.read()
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "ep-1"
    assert e.content == "Phase 0 Web MVP shipped 2026-06-14"
    assert e.layer == "L3"
    assert e.source == "hybrid"


def test_hybrid_write_appends_default_tags(adapter: HybridAdapter) -> None:
    """Writing without tags gets the adapter's default_tags appended."""
    m = Memory(id="ep-2", content="x", layer="L3", source="hybrid")
    adapter.write(m)
    target = next(e for e in adapter.read() if e.id == "ep-2")
    assert "#solomon" in target.tags


def test_hybrid_write_preserves_caller_tags(adapter: HybridAdapter) -> None:
    """If caller provides tags, default_tags are unioned in (not replacing)."""
    m = Memory(
        id="ep-3", content="x", layer="L3", source="hybrid",
        tags=["#custom"],
    )
    adapter.write(m)
    target = next(e for e in adapter.read() if e.id == "ep-3")
    assert set(target.tags) == {"#solomon", "#custom"}


def test_hybrid_read_filtered_by_project(adapter: HybridAdapter, hybrid_dir: Path) -> None:
    """read() only returns entries for this adapter's project."""
    a_fas = adapter
    a_other = HybridAdapter(storage_dir=hybrid_dir, project="OTHER", default_tags=[])
    a_fas.write(Memory(id="f-1", content="FAS fact", layer="L3", source="hybrid"))
    a_other.write(Memory(id="o-1", content="other fact", layer="L3", source="hybrid"))

    fas = a_fas.read()
    other = a_other.read()
    assert {e.id for e in fas} == {"f-1"}
    assert {e.id for e in other} == {"o-1"}


def test_hybrid_read_filtered_by_tag(adapter: HybridAdapter) -> None:
    """read(tag='X') returns only entries that have tag X."""
    adapter.write(Memory(id="a", content="x", layer="L3", source="hybrid",
                         tags=["#alpha"]))
    adapter.write(Memory(id="b", content="y", layer="L3", source="hybrid",
                         tags=["#beta"]))
    adapter.write(Memory(id="c", content="z", layer="L3", source="hybrid",
                         tags=["#alpha", "#beta"]))
    alpha_only = adapter.read(tag="#alpha")
    assert {e.id for e in alpha_only} == {"a", "c"}


# === Search ===

def test_hybrid_search_substring(adapter: HybridAdapter) -> None:
    """search() is a substring search over content (case-insensitive)."""
    adapter.write(Memory(id="s-1", content="MiniMax M2.7 supports 32 tools", layer="L3", source="hybrid"))
    adapter.write(Memory(id="s-2", content="GLM-4.7 used for cheap tasks", layer="L3", source="hybrid"))
    adapter.write(Memory(id="s-3", content="MiniMax streaming works", layer="L3", source="hybrid"))

    results = adapter.search("minimax")
    assert {r.id for r in results} == {"s-1", "s-3"}


def test_hybrid_search_scoped_to_project(adapter: HybridAdapter, hybrid_dir: Path) -> None:
    """search() does NOT leak across project boundaries."""
    a_other = HybridAdapter(storage_dir=hybrid_dir, project="OTHER", default_tags=[])
    adapter.write(Memory(id="f-1", content="FAS secret", layer="L3", source="hybrid"))
    a_other.write(Memory(id="o-1", content="FAS secret", layer="L3", source="hybrid"))  # same content, different project

    fas_results = adapter.search("FAS secret")
    other_results = a_other.search("FAS secret")
    assert {r.id for r in fas_results} == {"f-1"}
    assert {r.id for r in other_results} == {"o-1"}


# === Persistence ===

def test_hybrid_persists_across_instances(hybrid_dir: Path) -> None:
    """Writing through one adapter, reading through a fresh one works."""
    a1 = HybridAdapter(storage_dir=hybrid_dir, project="FAS", default_tags=["#solomon"])
    a1.write(Memory(id="persist-1", content="survives reload", layer="L3", source="hybrid"))
    a2 = HybridAdapter(storage_dir=hybrid_dir, project="FAS", default_tags=["#solomon"])
    loaded = a2.read()
    assert any(e.id == "persist-1" for e in loaded)


# === Provenance ===

def test_hybrid_provenance_appended(adapter: HybridAdapter) -> None:
    """Writing through hybrid appends an L3/hybrid hop."""
    m = Memory(
        id="prov-1", content="x", layer="L3", source="hybrid",
        provenance=[{"layer": "L1", "source": "hmem", "id": "upstream"}],
    )
    adapter.write(m)
    target = next(e for e in adapter.read() if e.id == "prov-1")
    assert len(target.provenance) == 2
    assert target.provenance[-1].layer == "L3"
    assert target.provenance[-1].source == "hybrid"
    assert target.provenance[-1].id == "prov-1"


# === Metadata round-trip ===

def test_hybrid_metadata_round_trip(adapter: HybridAdapter) -> None:
    """Free-form metadata dict survives write→read."""
    m = Memory(
        id="meta-1", content="x", layer="L3", source="hybrid",
        metadata={"project": "FAS", "tags": ["#solomon"], "episode_id": "e-42"},
    )
    adapter.write(m)
    target = next(e for e in adapter.read() if e.id == "meta-1")
    assert target.metadata["project"] == "FAS"
    assert target.metadata["episode_id"] == "e-42"


# === Recent ===

def test_hybrid_recent_returns_last_n(adapter: HybridAdapter) -> None:
    """recent(n) returns the last n entries in insertion order."""
    for i in range(5):
        adapter.write(Memory(id=f"r-{i}", content=f"v{i}", layer="L3", source="hybrid"))
    last3 = adapter.recent(n=3)
    assert [e.id for e in last3] == ["r-2", "r-3", "r-4"]


def test_hybrid_recent_zero_returns_empty(adapter: HybridAdapter) -> None:
    """recent(0) returns [] (not all)."""
    adapter.write(Memory(id="x", content="x", layer="L3", source="hybrid"))
    assert adapter.recent(0) == []
