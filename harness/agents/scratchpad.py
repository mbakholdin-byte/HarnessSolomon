"""Scratchpad dataclasses — per-session notes + plan steps (Phase 3 v1.2.0).

Phase 3 v1.2.0 introduces the "Write context" strategy from the
Anthropic context-engineering playbook: agents persist structured
notes (``Note``) and plan steps (``PlanStep``) across a session so the
context window carries curated state instead of the raw message log.

The two dataclasses mirror the Phase 3.5 :class:`~harness.agents.compact_store.CompactRecord`
shape (mutable for ``insert()``-assigned ``id`` / ``created_at``,
``to_row()`` / ``from_row()`` SQL marshalling, slots for memory economy).

The enums :class:`NoteLevel` and :class:`PlanStatus` are exposed as
``str``-valued ``Enum`` subclasses so they serialise to JSON natively
and round-trip through SQLite TEXT columns with the ``CHECK``
constraint matching the Python values.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# === Enums ===

class NoteLevel(str, Enum):
    """Stratification of note memory (Anthropic "Write context").

    * ``L0`` — hot layer, capped at ``scratchpad_l0_max_bytes`` bytes.
      Read into the system prompt on every turn (Phase 3 v1.2.1).
    * ``L1`` — per-session plan context. ~10KB. Read on demand.
    * ``L2`` — unbounded archive. Dense+BM25 retrieval in Phase 3 v1.3.0.
    """

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class PlanStatus(str, Enum):
    """Lifecycle state of a :class:`PlanStep`.

    The CHECK constraint on ``plan_steps.status`` matches these
    values exactly; an unknown value will fail the SQL insert.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


# === Note ===

@dataclass(slots=True)
class Note:
    """One row of the ``scratchpad_notes`` table.

    Mutated by :meth:`harness.agents.scratchpad_store.ScratchpadStore.write_note`
    to fill the assigned ``id`` and ``created_at`` after insert
    (mirror :class:`~harness.agents.compact_store.CompactRecord`).
    ``tags`` is stored as JSON string in SQLite, decoded on read.
    """

    session_id: str
    level: NoteLevel
    content: str
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    id: int = 0
    agent_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        """Serialise to the SQL column shape used by INSERT."""
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "level": self.level.value,
            "content": self.content,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Note:
        """Rehydrate from a SELECT row. Inverse of :meth:`to_row`."""
        raw_tags = row["tags"]
        if isinstance(raw_tags, (bytes, bytearray)):
            raw_tags = raw_tags.decode("utf-8")
        return cls(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            agent_id=row["agent_id"] if row["agent_id"] is not None else None,
            level=NoteLevel(str(row["level"])),
            content=str(row["content"]),
            tags=json.loads(raw_tags) if raw_tags else [],
            created_at=float(row["created_at"]),
        )


# === PlanStep ===

@dataclass(slots=True)
class PlanStep:
    """One row of the ``plan_steps`` table.

    ``deps`` is a JSON list of ``int`` step-ids that must be ``DONE``
    before this step can start. The LLM is responsible for emitting
    the dependency graph via the ``scratchpad_plan_step`` tool;
    :class:`harness.agents.scratchpad_store.ScratchpadStore` does not
    enforce the ordering.
    """

    session_id: str
    description: str
    status: PlanStatus = PlanStatus.PENDING
    deps: list[int] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    id: int = 0
    agent_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "description": self.description,
            "status": self.status.value,
            "deps": json.dumps(self.deps, ensure_ascii=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PlanStep:
        raw_deps = row["deps"]
        if isinstance(raw_deps, (bytes, bytearray)):
            raw_deps = raw_deps.decode("utf-8")
        return cls(
            id=int(row["id"]),
            session_id=str(row["session_id"]),
            agent_id=row["agent_id"] if row["agent_id"] is not None else None,
            description=str(row["description"]),
            status=PlanStatus(str(row["status"])),
            deps=json.loads(raw_deps) if raw_deps else [],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
