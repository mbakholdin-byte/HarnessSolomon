"""Tests for the file adapter (Phase 1, Step 5).

The file adapter is the L4 (file / Markdown) storage. It is the
**source of truth** for offline / human-review use. One Markdown
file per memory entry (YAML frontmatter + body), plus a single
``INDEX.md`` that lists every entry (id, title, layer, ts).

This is the storage that maps onto Obsidian / MarkObsidian.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from harness.memory.adapters.file import FileAdapter
from harness.memory.schema import Memory


# === Fixtures ===

@pytest.fixture
def mem_dir(tmp_path: Path) -> Path:
    d = tmp_path / "mem"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def adapter(mem_dir: Path) -> FileAdapter:
    return FileAdapter(memory_dir=mem_dir)


# === Construction ===

def test_file_creates_dir(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    FileAdapter(memory_dir=target)
    assert target.exists()


def test_file_default_dir() -> None:
    a = FileAdapter()
    assert a.memory_dir.exists()


# === Write → file ===

def test_file_write_creates_markdown_with_frontmatter(adapter: FileAdapter, mem_dir: Path) -> None:
    """A Memory is written as one .md file with YAML frontmatter + body."""
    m = Memory(
        id="note-1",
        content="First note content",
        layer="L4",
        source="file",
        tags=["#solomon"],
    )
    adapter.write(m)

    # Two .md files exist: the note and INDEX.md
    md = sorted(mem_dir.glob("*.md"))
    assert len(md) == 2
    # The note file (not the index) carries the content
    note = next(p for p in md if p.name == "note-1.md")
    text = note.read_text(encoding="utf-8")
    # YAML frontmatter delimiters
    assert text.startswith("---\n")
    assert "\n---\n" in text
    # Frontmatter must include the canonical fields
    assert 'id: "note-1"' in text
    assert 'layer: "L4"' in text
    assert 'source: "file"' in text
    # Body is the content
    assert "First note content" in text


def test_file_write_creates_index(adapter: FileAdapter, mem_dir: Path) -> None:
    """After the first write, INDEX.md exists and lists the entry."""
    adapter.write(Memory(id="idx-1", content="x", layer="L4", source="file"))
    index = mem_dir / "INDEX.md"
    assert index.exists()
    text = index.read_text(encoding="utf-8")
    assert "idx-1" in text


def test_file_index_lists_all_entries(adapter: FileAdapter, mem_dir: Path) -> None:
    """INDEX.md lists every entry by id."""
    for i in range(3):
        adapter.write(Memory(id=f"e-{i}", content=f"v{i}", layer="L4", source="file"))
    text = (mem_dir / "INDEX.md").read_text(encoding="utf-8")
    assert "e-0" in text
    assert "e-1" in text
    assert "e-2" in text


# === Read ===

def test_file_read_returns_all(adapter: FileAdapter) -> None:
    """read() returns every Memory stored in the dir."""
    for i in range(3):
        adapter.write(Memory(id=f"r-{i}", content=f"v{i}", layer="L4", source="file"))
    entries = adapter.read()
    assert {e.id for e in entries} == {"r-0", "r-1", "r-2"}


def test_file_read_by_id(adapter: FileAdapter) -> None:
    """get(id) returns one Memory by id; missing → None."""
    adapter.write(Memory(id="g-1", content="x", layer="L4", source="file"))
    m = adapter.get("g-1")
    assert m is not None
    assert m.content == "x"
    assert adapter.get("nope") is None


def test_file_read_empty(adapter: FileAdapter) -> None:
    """read() on an empty dir returns []."""
    assert adapter.read() == []


# === Filename safety ===

def test_file_write_sanitises_id(adapter: FileAdapter, mem_dir: Path) -> None:
    """Path-traversal-style ids get sanitised into a safe filename."""
    adapter.write(Memory(id="../../etc/passwd", content="evil", layer="L4", source="file"))
    # The file must end up INSIDE the memory_dir (plus INDEX.md)
    files = list(mem_dir.glob("*.md"))
    assert all(f.parent == mem_dir for f in files)
    # 1 entry + 1 INDEX.md
    entry_files = [f for f in files if f.name != "INDEX.md"]
    assert len(entry_files) == 1


def test_file_filename_is_deterministic(adapter: FileAdapter, mem_dir: Path) -> None:
    """Same id → same filename across writes (overwrite, not duplicate)."""
    adapter.write(Memory(id="same", content="v1", layer="L4", source="file"))
    adapter.write(Memory(id="same", content="v2", layer="L4", source="file"))
    files = [f for f in mem_dir.glob("*.md") if f.name != "INDEX.md"]
    assert len(files) == 1
    assert "v2" in files[0].read_text(encoding="utf-8")


# === Tags round-trip ===

def test_file_tags_round_trip(adapter: FileAdapter) -> None:
    """Tags survive write→read."""
    m = Memory(
        id="t-1", content="x", layer="L4", source="file",
        tags=["#solomon", "#harness", "#phase-1"],
    )
    adapter.write(m)
    loaded = adapter.get("t-1")
    assert loaded is not None
    assert set(loaded.tags) == {"#solomon", "#harness", "#phase-1"}


# === Metadata ===

def test_file_metadata_round_trip(adapter: FileAdapter) -> None:
    """Free-form metadata dict survives write→read."""
    m = Memory(
        id="m-1", content="x", layer="L4", source="file",
        metadata={"obsidian_uri": "obsidian://open?vault=MarkObsidian&file=note"},
    )
    adapter.write(m)
    loaded = adapter.get("m-1")
    assert loaded is not None
    assert loaded.metadata.get("obsidian_uri", "").startswith("obsidian://")


# === Search ===

def test_file_search_substring(adapter: FileAdapter) -> None:
    """search() returns entries whose content contains the query (case-insensitive)."""
    adapter.write(Memory(id="a", content="MiniMax M2.7 supports 32 tools", layer="L4", source="file"))
    adapter.write(Memory(id="b", content="Lesson: parallel subagents", layer="L4", source="file"))
    results = adapter.search("minimax")
    assert {r.id for r in results} == {"a"}


def test_file_search_by_tag(adapter: FileAdapter) -> None:
    """search(tag='#X') returns entries carrying that tag."""
    adapter.write(Memory(id="a", content="x", layer="L4", source="file", tags=["#alpha"]))
    adapter.write(Memory(id="b", content="y", layer="L4", source="file", tags=["#beta"]))
    results = adapter.search(tag="#alpha")
    assert {r.id for r in results} == {"a"}


# === Provenance ===

def test_file_provenance_appended(adapter: FileAdapter) -> None:
    """Writing through file appends an L4/file hop to provenance."""
    m = Memory(
        id="p-1", content="x", layer="L4", source="file",
        provenance=[{"layer": "L1", "source": "hmem", "id": "up"}],
    )
    adapter.write(m)
    loaded = adapter.get("p-1")
    assert loaded is not None
    assert len(loaded.provenance) == 2
    assert loaded.provenance[-1].layer == "L4"
    assert loaded.provenance[-1].source == "file"


# === Index update on rewrite ===

def test_file_index_dedupes_on_rewrite(adapter: FileAdapter, mem_dir: Path) -> None:
    """Rewriting the same id doesn't duplicate INDEX.md entries."""
    adapter.write(Memory(id="dup", content="v1", layer="L4", source="file"))
    adapter.write(Memory(id="dup", content="v2", layer="L4", source="file"))
    text = (mem_dir / "INDEX.md").read_text(encoding="utf-8")
    # 'dup' appears in index only once
    assert text.count("dup") == 1
