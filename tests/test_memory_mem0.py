"""Tests for the mem0 memory adapter (Phase 1, Step 3).

The mem0 adapter is the L2 (semantic) storage. It reads/writes
``Memory`` records from/to a key-value store where the canonical key
is the user_id (Solomon uses ``user_id="solomon"``) and each entry
is a semantic fact with an embedding.

We use a file-backed fake for unit tests (one JSONL file per
user). In production, this maps onto either:
  - the ``mcp__mem0__*`` MCP server (preferred), or
  - a direct Qdrant collection ``solomon-memories`` (legacy).

Both paths conform to the same ``Mem0Adapter`` API.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.memory.adapters.mem0 import Mem0Adapter
from harness.memory.schema import Memory


# === Fixtures ===

@pytest.fixture
def mem0_dir(tmp_path: Path) -> Path:
    """Tmp dir for the fake mem0 backend."""
    d = tmp_path / "mem0-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def adapter(mem0_dir: Path) -> Mem0Adapter:
    """File-backed Mem0Adapter for solomon user."""
    return Mem0Adapter(
        storage_dir=mem0_dir,
        user_id="solomon",
        collection="solomon-memories",
    )


# === Construction ===

def test_mem0_adapter_creates_dir(tmp_path: Path) -> None:
    """Constructor creates the storage dir if missing."""
    target = tmp_path / "fresh" / "dir"
    Mem0Adapter(storage_dir=target, user_id="solomon")
    assert target.exists()


def test_mem0_default_storage_dir() -> None:
    """When no storage_dir is given, the adapter uses a sensible default."""
    a = Mem0Adapter(user_id="solomon")
    assert a.storage_dir.exists()
    # The default must be writable from CI
    assert a.storage_dir.is_dir()


def test_mem0_user_id_required() -> None:
    """user_id must be a non-empty string."""
    with pytest.raises(ValueError):
        Mem0Adapter(user_id="", storage_dir=Path("/tmp"))
    with pytest.raises(ValueError):
        Mem0Adapter(user_id=None, storage_dir=Path("/tmp"))  # type: ignore[arg-type]


# === Write → Read ===

def test_mem0_write_and_read(adapter: Mem0Adapter) -> None:
    """Round-trip: write then read returns the same Memory."""
    m = Memory(
        id="fact-1",
        content="User prefers concise answers",
        layer="L2",
        source="mem0",
        confidence=0.85,
        tags=["#preference"],
    )
    adapter.write(m)
    entries = adapter.read()
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "fact-1"
    assert e.content == "User prefers concise answers"
    assert e.layer == "L2"
    assert e.source == "mem0"
    assert e.confidence == 0.85


def test_mem0_read_filtered_by_user(adapter: Mem0Adapter, mem0_dir: Path) -> None:
    """read() only returns entries for THIS user; other users are invisible."""
    a_solomon = adapter
    a_alex = Mem0Adapter(
        storage_dir=mem0_dir, user_id="alex", collection="alex-memories"
    )
    a_solomon.write(Memory(id="s-1", content="solomon fact", layer="L2", source="mem0"))
    a_alex.write(Memory(id="a-1", content="alex fact", layer="L2", source="mem0"))

    solomon_facts = a_solomon.read()
    assert {f.id for f in solomon_facts} == {"s-1"}
    alex_facts = a_alex.read()
    assert {f.id for f in alex_facts} == {"a-1"}


def test_mem0_read_empty_returns_empty(adapter: Mem0Adapter) -> None:
    """read() on a fresh store returns []."""
    assert adapter.read() == []


def test_mem0_write_overwrites_by_id(adapter: Mem0Adapter) -> None:
    """Writing a Memory with an existing id REPLACES the old one (mem0 upsert)."""
    adapter.write(Memory(id="dup-1", content="v1", layer="L2", source="mem0"))
    adapter.write(Memory(id="dup-1", content="v2", layer="L2", source="mem0"))
    entries = adapter.read()
    assert len(entries) == 1
    assert entries[0].content == "v2"


def test_mem0_delete_by_id(adapter: Mem0Adapter) -> None:
    """delete() removes the entry by id; missing id is a no-op."""
    adapter.write(Memory(id="del-1", content="x", layer="L2", source="mem0"))
    adapter.write(Memory(id="del-2", content="y", layer="L2", source="mem0"))

    assert adapter.delete("del-1") is True
    assert {f.id for f in adapter.read()} == {"del-2"}

    # Idempotent
    assert adapter.delete("del-1") is False
    assert adapter.delete("never-existed") is False


# === Search ===

def test_mem0_search_substring(adapter: Mem0Adapter) -> None:
    """search() returns (Memory, score) tuples whose content matches."""
    adapter.write(Memory(id="s-1", content="prefers Python over JS", layer="L2", source="mem0"))
    adapter.write(Memory(id="s-2", content="uses FastAPI for HTTP", layer="L2", source="mem0"))
    adapter.write(Memory(id="s-3", content="prefers concise answers", layer="L2", source="mem0"))

    results = adapter.search("prefers")
    ids = {mem.id for mem, _ in results}
    assert ids == {"s-1", "s-3"}

    # Case-insensitive
    results2 = adapter.search("FASTAPI")
    ids2 = {mem.id for mem, _ in results2}
    assert ids2 == {"s-2"}


def test_mem0_search_returns_scored(adapter: Mem0Adapter) -> None:
    """search() returns a list of (Memory, score) tuples, score in [0, 1]."""
    adapter.write(Memory(id="sc-1", content="x", layer="L2", source="mem0"))
    results = adapter.search("x")
    assert len(results) == 1
    mem, score = results[0]
    assert mem.id == "sc-1"
    assert 0.0 <= score <= 1.0


# === Metadata round-trip ===

def test_mem0_metadata_round_trip(adapter: Mem0Adapter) -> None:
    """Free-form metadata dict survives write→read."""
    m = Memory(
        id="meta-1", content="x", layer="L2", source="mem0",
        metadata={"mem0_user_id": "solomon", "score": 0.92, "raw": {"a": 1}},
    )
    adapter.write(m)
    loaded = adapter.read()
    target = next(e for e in loaded if e.id == "meta-1")
    assert target.metadata["mem0_user_id"] == "solomon"
    assert target.metadata["score"] == 0.92
    assert target.metadata["raw"] == {"a": 1}


# === Provenance ===

def test_mem0_provenance_appended(adapter: Mem0Adapter) -> None:
    """Writing through mem0 appends an L2/mem0 hop to provenance."""
    m = Memory(
        id="prov-1", content="x", layer="L2", source="mem0",
        provenance=[{"layer": "L1", "source": "hmem", "id": "upstream"}],
    )
    adapter.write(m)
    loaded = adapter.read()
    target = next(e for e in loaded if e.id == "prov-1")
    assert len(target.provenance) == 2
    assert target.provenance[0].layer == "L1"
    assert target.provenance[-1].layer == "L2"
    assert target.provenance[-1].source == "mem0"
    assert target.provenance[-1].id == "prov-1"


# === Tags ===

def test_mem0_tags_round_trip(adapter: Mem0Adapter) -> None:
    """Tags survive write→read."""
    m = Memory(
        id="t-1", content="x", layer="L2", source="mem0",
        tags=["#solomon", "#preference"],
    )
    adapter.write(m)
    target = next(e for e in adapter.read() if e.id == "t-1")
    assert set(target.tags) == {"#solomon", "#preference"}


# === Persistence ===

def test_mem0_persists_across_instances(mem0_dir: Path) -> None:
    """Writing through one adapter, reading through a fresh one works."""
    a1 = Mem0Adapter(storage_dir=mem0_dir, user_id="solomon")
    a1.write(Memory(id="persist-1", content="survives", layer="L2", source="mem0"))
    a2 = Mem0Adapter(storage_dir=mem0_dir, user_id="solomon")
    loaded = a2.read()
    assert any(e.id == "persist-1" for e in loaded)
