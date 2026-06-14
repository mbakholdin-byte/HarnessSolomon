"""Solomon Harness — unified Memory schema (Phase 1, Step 1).

The single canonical shape that all four memory adapters (hmem, mem0,
mempalace, hybrid, file) accept and emit. The schema is intentionally
storage-agnostic — adapters translate to/from their native format.

Layer semantics (from Solomon canon — see
``C:/MyAI/_Solomon/.claude/rules/MEMORY.md``):

  - **L1 (hmem)**:       Hierarchical / structured knowledge. Prefixed
                         entries (P/L/D/E/M). Fast structured lookups.
  - **L2 (mem0)**:       Semantic / fuzzy. User preferences, facts.
  - **L2.5 (mempalace)**: Knowledge-graph (wings / rooms / drawers /
                          closets). Triples + structural memory.
  - **L3 (hybrid)**:     Episodic / artifacts. Qdrant + SQLite +
                         OpenSearch. Sessions, episodes, multi-modal
                         sweeps.
  - **L4 (file)**:       Markdown + INDEX.md + Obsidian vault. Human-
                         readable source of truth.

Source semantics — which adapter produced the entry:
  - ``hmem``:        came from hmem hierarchical memory
  - ``mem0``:        came from mem0 semantic memory
  - ``mempalace``:   came from mempalace KG
  - ``hybrid``:      came from fas-hybrid-memory
  - ``file``:        came from a file/Markdown source
  - ``manual``:      user-typed / outside any automated layer
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# === Public constants ===

#: All canonical layer ids. Use this in code that iterates layers or
#: shows a layer chooser in the UI.
ALL_LAYERS: tuple[str, ...] = ("L1", "L2", "L2.5", "L3", "L4")

#: Max entries in a single Memory's provenance chain. Older entries
#: are dropped on append (FIFO). Keeps the payload bounded and JSON
#: round-trip cheap.
PROVENANCE_CHAIN_MAX: int = 8


# === Type aliases (for adapter signatures) ===

#: Memory layer — L1/L2/L2.5/L3/L4. L2.5 is the intermediate mempalace
#: KG layer per Solomon canon.
MemoryLayer = Literal["L1", "L2", "L2.5", "L3", "L4"]

#: Origin of a memory entry. Maps 1:1 to adapters (plus "manual" for
#: user-typed entries). Adapters SHOULD set this to their own id when
#: writing; the unified facade sets it on read.
MemorySource = Literal["hmem", "mem0", "mempalace", "hybrid", "file", "manual"]


# === Helpers ===

def _now() -> datetime:
    """UTC now, naive (drop tzinfo for JSONL/SQLite consistency)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _uuid() -> str:
    """UUID4 string. Stable across the process."""
    return str(uuid4())


# === Models ===

class ProvenanceEntry(BaseModel):
    """One hop in a memory's provenance chain.

    Tracks which layer/source/id produced (or transformed) the entry
    on its way to the current storage. Lets us trace ``m-001 came
    from hmem#h-42 which was originally written by mem0#m-17``.
    """

    model_config = ConfigDict(extra="ignore")

    layer: str
    source: str
    id: str


class Memory(BaseModel):
    """A single unit of memory.

    The shape is deliberately close to a mem0 record + a hmem prefix
    entry, with extras (provenance chain, links, tags, metadata) to
    bridge the differences between the four layers.

    Idempotency: ``id`` is the canonical primary key. Hashable by id
    only (so two memories with the same id dedup in sets), but full
    Pydantic equality still requires all fields to match.
    """

    model_config = ConfigDict(extra="ignore")

    # === Identity ===
    id: str = Field(default_factory=_uuid)
    ts: datetime = Field(default_factory=_now)

    # === Core ===
    content: str
    layer: MemoryLayer
    source: MemorySource

    # === Trust / lifecycle ===
    #: 0.0 (low) .. 1.0 (high). Default 1.0 = full confidence.
    confidence: float = 1.0
    #: Time-to-live in seconds. None = infinite (do not expire).
    ttl: int | None = None

    # === Graph ===
    #: Ordered list of layer/source/id hops that produced this entry.
    #: Newest hop is last. Capped at ``PROVENANCE_CHAIN_MAX`` entries.
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    #: Bidirectional links to other memory ids (related facts, sources).
    links: list[str] = Field(default_factory=list)
    #: Free-form tags, e.g. ``["#solomon", "#harness", "#phase-0.6"]``.
    tags: list[str] = Field(default_factory=list)

    # === Adapter-specific ===
    #: Free-form dict for adapter extensions. Examples:
    #: hmem adapter: ``{"prefix": "L", "key": "tuning-2026-06"}``
    #: mem0 adapter: ``{"mem0_user_id": "solomon", "score": 0.92}``
    #: hybrid:       ``{"project": "FAS", "tags": ["#solomon"]}``
    metadata: dict[str, Any] = Field(default_factory=dict)

    # === Validators ===

    @field_validator("content")
    @classmethod
    def _content_not_empty(cls, v: str) -> str:
        if not isinstance(v, str):
            raise TypeError("content must be a string")
        if not v:
            raise ValueError("content must be a non-empty string")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            raise TypeError("confidence must be a number")
        fv = float(v)
        if fv < 0.0 or fv > 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {fv}")
        return fv

    @field_validator("ttl")
    @classmethod
    def _ttl_positive_or_none(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise TypeError("ttl must be an int (seconds) or None")
        if v <= 0:
            raise ValueError(f"ttl must be > 0 seconds (None = infinite), got {v}")
        return v

    @field_validator("provenance")
    @classmethod
    def _provenance_capped(cls, v: list[ProvenanceEntry]) -> list[ProvenanceEntry]:
        if len(v) > PROVENANCE_CHAIN_MAX:
            raise ValueError(
                f"provenance chain exceeds max length "
                f"({PROVENANCE_CHAIN_MAX} entries), got {len(v)}"
            )
        return v

    # === Identity & equality ===

    def __hash__(self) -> int:
        """Hash by id only (lets Memory dedup in sets/dicts by primary key)."""
        return hash(self.id)

    # === JSONL helpers (L4 file adapter compatibility) ===

    def to_jsonl(self) -> str:
        """Serialize to a single-line JSON string for JSONL storage.

        No embedded newlines — safe to write to a JSONL file as one
        record per line.
        """
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> "Memory":
        """Reverse ``to_jsonl()``. Strips trailing whitespace/newline first."""
        return cls.model_validate_json(line.strip())


__all__ = [
    "ALL_LAYERS",
    "PROVENANCE_CHAIN_MAX",
    "MemoryLayer",
    "MemorySource",
    "Memory",
    "ProvenanceEntry",
]


# === Module-level JSON hook ===
# (helps the test_to_jsonl_line assertion: json.dumps with default
# settings doesn't escape \n; we rely on Pydantic's encoder to produce
# \\n in the JSON string. Re-exported for the file adapter.)
def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for non-serialisable objects (datetime etc.)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


# Bind into json for convenience; re-exported in __all__ if needed.
json_default = _json_default
