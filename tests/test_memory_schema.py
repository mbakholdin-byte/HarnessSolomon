"""Tests for the unified Memory schema (Phase 1, Step 1).

The schema is the canonical shape that all four adapters (hmem, mem0,
hybrid, file) accept and emit. It does NOT depend on any storage —
just Pydantic validation + JSON round-trip + provenance tracking.

TDD: write tests first, then implement.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from harness.memory.schema import (
    Memory,
    MemoryLayer,
    MemorySource,
    PROVENANCE_CHAIN_MAX,
)


# === Construction ===

def test_memory_minimal_construction() -> None:
    """Memory with only required fields — id, content, layer, source, ts."""
    m = Memory(
        id="abc-123",
        content="User prefers concise answers",
        layer="L2",  # type: ignore[arg-type]
        source="mem0",  # type: ignore[arg-type]
    )
    assert m.id == "abc-123"
    assert m.content == "User prefers concise answers"
    assert m.layer == "L2"
    assert m.source == "mem0"
    # Defaults
    assert m.confidence == 1.0
    assert m.ttl is None  # infinite by default
    assert m.provenance == []
    assert m.links == []
    assert m.tags == []


def test_memory_id_auto_generated() -> None:
    """id is auto-generated if omitted (UUID4)."""
    m1 = Memory(content="x", layer="L1", source="hmem")
    m2 = Memory(content="x", layer="L1", source="hmem")
    # Each is a valid UUID4
    uuid.UUID(m1.id)
    uuid.UUID(m2.id)
    # Two constructions → two different ids
    assert m1.id != m2.id


def test_memory_ts_auto_generated() -> None:
    """ts defaults to UTC now when omitted."""
    before = datetime.now(UTC).replace(tzinfo=None)
    m = Memory(content="x", layer="L1", source="hmem")
    after = datetime.now(UTC).replace(tzinfo=None)
    # 1 second slack on each side
    assert (after - before).total_seconds() < 2.0
    assert before <= m.ts <= after


def test_memory_full_construction() -> None:
    """All optional fields populated."""
    m = Memory(
        id="m-001",
        content="MiniMax max_tools is actually 32, not 4",
        layer="L2",
        source="mem0",
        ts=datetime(2026, 6, 14, 12, 0, 0),
        confidence=0.85,
        ttl=86400,  # 1 day
        provenance=[
            {"layer": "L3", "source": "hybrid", "id": "h-99"},
        ],
        links=["m-002", "m-003"],
        tags=["#solomon", "#harness", "#phase-0.6"],
    )
    assert m.confidence == 0.85
    assert m.ttl == 86400
    assert len(m.provenance) == 1
    assert m.provenance[0].layer == "L3"
    assert m.provenance[0].source == "hybrid"
    assert m.provenance[0].id == "h-99"
    assert m.links == ["m-002", "m-003"]
    assert "#solomon" in m.tags


# === Validation ===

def test_memory_layer_must_be_l1_l4() -> None:
    """layer is one of L1/L2/L3/L4 (and the intermediate L2.5 for mempalace)."""
    # Valid
    for layer in ("L1", "L2", "L2.5", "L3", "L4"):
        m = Memory(content="x", layer=layer, source="hmem")  # type: ignore[arg-type]
        assert m.layer == layer

    # Invalid
    with pytest.raises(ValidationError):
        Memory(content="x", layer="L9", source="hmem")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Memory(content="x", layer="l1", source="hmem")  # lowercase


def test_memory_source_must_be_known() -> None:
    """source is one of the 4 adapters + 'manual' for user-typed entries."""
    for src in ("hmem", "mem0", "mempalace", "hybrid", "file", "manual"):
        m = Memory(content="x", layer="L1", source=src)  # type: ignore[arg-type]
        assert m.source == src

    with pytest.raises(ValidationError):
        Memory(content="x", layer="L1", source="unknown-source")  # type: ignore[arg-type]


def test_memory_confidence_in_range() -> None:
    """confidence is 0.0 .. 1.0."""
    # Valid
    Memory(content="x", layer="L1", source="hmem", confidence=0.0)
    Memory(content="x", layer="L1", source="hmem", confidence=1.0)
    Memory(content="x", layer="L1", source="hmem", confidence=0.5)

    # Invalid
    with pytest.raises(ValidationError):
        Memory(content="x", layer="L1", source="hmem", confidence=-0.1)
    with pytest.raises(ValidationError):
        Memory(content="x", layer="L1", source="hmem", confidence=1.5)


def test_memory_content_not_empty() -> None:
    """content must be a non-empty string."""
    m = Memory(content=" ", layer="L1", source="hmem")
    # Note: " " is technically non-empty; only "" raises
    assert m.content == " "

    with pytest.raises(ValidationError):
        Memory(content="", layer="L1", source="hmem")


def test_memory_ttl_must_be_positive_if_set() -> None:
    """ttl (seconds until expiry) is None (infinite) or a positive int."""
    Memory(content="x", layer="L1", source="hmem", ttl=None)
    Memory(content="x", layer="L1", source="hmem", ttl=1)
    Memory(content="x", layer="L1", source="hmem", ttl=86400 * 365)

    with pytest.raises(ValidationError):
        Memory(content="x", layer="L1", source="hmem", ttl=0)
    with pytest.raises(ValidationError):
        Memory(content="x", layer="L1", source="hmem", ttl=-1)


# === Provenance ===

def test_memory_provenance_chain_length_capped() -> None:
    """Provenance chain is capped at PROVENANCE_CHAIN_MAX entries."""
    # 5 entries is OK
    prov = [
        {"layer": "L1", "source": "hmem", "id": f"id-{i}"}
        for i in range(PROVENANCE_CHAIN_MAX)
    ]
    m = Memory(content="x", layer="L1", source="hmem", provenance=prov)
    assert len(m.provenance) == PROVENANCE_CHAIN_MAX

    # PROVENANCE_CHAIN_MAX + 1 is rejected
    prov_over = [
        {"layer": "L1", "source": "hmem", "id": f"id-{i}"}
        for i in range(PROVENANCE_CHAIN_MAX + 1)
    ]
    with pytest.raises(ValidationError):
        Memory(content="x", layer="L1", source="hmem", provenance=prov_over)


def test_memory_provenance_entry_shape() -> None:
    """Each provenance entry must have layer, source, id."""
    # Missing field → ValidationError
    with pytest.raises(ValidationError):
        Memory(
            content="x", layer="L1", source="hmem",
            provenance=[{"layer": "L1", "source": "hmem"}],  # no id
        )


# === Serialization ===

def test_memory_to_dict_round_trip() -> None:
    """Memory.model_dump() → dict, then Memory(**dict) reconstructs equal object."""
    m = Memory(
        id="m-001",
        content="Hello",
        layer="L2",
        source="mem0",
        confidence=0.7,
        tags=["#test"],
    )
    d = m.model_dump()
    assert isinstance(d, dict)
    m2 = Memory(**d)
    assert m2 == m


def test_memory_to_json_round_trip() -> None:
    """Memory.model_dump_json() → str, then Memory.model_validate_json() round-trips."""
    m = Memory(
        id="m-002",
        content="World",
        layer="L3",
        source="hybrid",
        confidence=0.9,
        provenance=[{"layer": "L1", "source": "hmem", "id": "x"}],
        links=["m-001"],
    )
    js = m.model_dump_json()
    m2 = Memory.model_validate_json(js)
    assert m2 == m


def test_memory_to_jsonl_line() -> None:
    """to_jsonl() returns a single line (no embedded newlines)."""
    m = Memory(
        id="m-003",
        content="line1\nline2",  # embedded newline
        layer="L4",
        source="file",
    )
    line = m.to_jsonl()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["content"] == "line1\nline2"


def test_memory_from_jsonl_line() -> None:
    """from_jsonl() reverses to_jsonl()."""
    m = Memory(id="m-004", content="X", layer="L1", source="hmem")
    m2 = Memory.from_jsonl(m.to_jsonl())
    assert m2 == m


# === Helpers ===

def test_memory_layer_constants_exposed() -> None:
    """MemoryLayer Literal / type alias is importable for adapters to use."""
    # Type-level: just check the constant exists
    assert MemoryLayer is not None
    # The four canonical layers + the intermediate mempalace one
    valid = {"L1", "L2", "L2.5", "L3", "L4"}
    # We just verify the literal accepts the same set (type-check only)
    _m: Memory = Memory(content="x", layer="L1", source="hmem")
    assert _m.layer in valid


def test_memory_source_constants_exposed() -> None:
    """MemorySource Literal is importable."""
    assert MemorySource is not None


def test_memory_layer_values_match_solomon_canon() -> None:
    """Layer values match the 4-layer Solomon canon from CLAUDE.md + SOLOMON.md.

    L1 = hmem (hierarchical), L2 = mem0 (semantic) + L2.5 mempalace (KG),
    L3 = fas-hybrid (episodic), L4 = file (markdown / Obsidian).
    """
    # The exact values are exposed as a tuple for programmatic checks
    from harness.memory.schema import ALL_LAYERS
    assert "L1" in ALL_LAYERS
    assert "L2" in ALL_LAYERS
    assert "L2.5" in ALL_LAYERS
    assert "L3" in ALL_LAYERS
    assert "L4" in ALL_LAYERS


# === Metadata ===

def test_memory_metadata_pass_through() -> None:
    """Free-form metadata dict is allowed for adapter-specific extensions."""
    m = Memory(
        content="x", layer="L1", source="hmem",
        metadata={"hmem_prefix": "L", "episode_id": "e-42"},
    )
    assert m.metadata["hmem_prefix"] == "L"
    assert m.metadata["episode_id"] == "e-42"


def test_memory_metadata_default_empty_dict() -> None:
    """metadata defaults to {} when omitted."""
    m = Memory(content="x", layer="L1", source="hmem")
    assert m.metadata == {}


# === Equality ===

def test_memory_equality_by_id_only_for_hash() -> None:
    """Two Memory objects with same id hash equal (id is the canonical key)."""
    m1 = Memory(id="same", content="A", layer="L1", source="hmem")
    m2 = Memory(id="same", content="B", layer="L2", source="mem0")
    # Hashable by id (used for dedup sets)
    assert hash(m1) == hash(m2)
    # But full equality (model_eq) requires all fields to match
    assert m1 != m2

    # Same id, same content → fully equal
    m3 = Memory(id="same", content="A", layer="L1", source="hmem")
    assert m1 == m3
