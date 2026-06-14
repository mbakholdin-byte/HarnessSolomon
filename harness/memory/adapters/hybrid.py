"""fas-hybrid-memory adapter — L3 episodic memory (Phase 1, Step 4).

The hybrid layer stores episodic / artifact memory: sessions,
multi-modal sweeps, research results. Scoped by ``project`` and
``tags``. Solomon canon: ``project="FAS"``, ``tags=["#solomon"]``.

Production backend is fas-hybrid-memory (Qdrant + SQLite +
OpenSearch). This adapter provides a SQLite-only fallback that
conforms to the same ``HybridAdapter`` API. The schema is
forward-compatible: a real Qdrant/OpenSearch adapter can be added
later without changing the interface.

The on-disk file is SQLite; ``read()`` is full-table scan. For
production, swap in a vector search via Qdrant. The fallback is
intentionally simple — the unified facade is the place that adds
BM25+vector retrieval.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from harness.memory.schema import (
    PROVENANCE_CHAIN_MAX,
    Memory,
    ProvenanceEntry,
)

logger = logging.getLogger(__name__)


# === Constants ===

#: Env var to override the default storage dir.
ENV_STORAGE_DIR: str = "SOLOMON_HYBRID_DIR"

#: Default project (Solomon canon).
DEFAULT_PROJECT: str = "FAS"

#: Default tag (every Solomon hybrid entry carries this).
DEFAULT_TAG: str = "#solomon"


# === Helpers ===

def _default_dir() -> Path:
    """Default storage dir for the SQLite-backed fallback.

    Priority: ``$SOLOMON_HYBRID_DIR`` → system temp ``/solomon-hybrid``
    (Windows) / ``~/.solomon/hybrid`` (Unix) → ``./data/hybrid``.
    """
    env = os.environ.get(ENV_STORAGE_DIR, "").strip()
    if env:
        return Path(env)
    home = Path(os.path.expanduser("~"))
    if os.name == "nt":
        import tempfile
        return Path(tempfile.gettempdir()) / "solomon-hybrid"
    return home / ".solomon" / "hybrid"


def _safe_filename(s: str) -> str:
    safe = "".join(c for c in s if c.isalnum() or c in ("-", "_"))
    if not safe or safe in (".", ".."):
        safe = "default"
    return safe


# === Adapter ===

class HybridAdapter:
    """SQLite-backed L3 episodic memory adapter.

    Args:
        storage_dir:    Directory holding ``<project>.sqlite3`` DBs.
                        Created if missing.
        project:        Project scope (default ``"FAS"`` per Solomon
                        canon). Each project gets its own DB file.
        default_tags:   Tags added to every entry that doesn't carry
                        them (default ``["#solomon"]``).
    """

    def __init__(
        self,
        storage_dir: Path | str | None = None,
        project: str = DEFAULT_PROJECT,
        default_tags: list[str] | None = None,
    ) -> None:
        if storage_dir is None:
            self.storage_dir = _default_dir()
        else:
            self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.project = project
        self.default_tags = list(default_tags) if default_tags else [DEFAULT_TAG]
        self._db = self.storage_dir / f"{_safe_filename(project)}.sqlite3"
        self._ensure_schema()
        logger.debug(
            "HybridAdapter: project=%s db=%s default_tags=%s",
            project, self._db, self.default_tags,
        )

    # --- internal ---

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        """Create the episodes table if missing. Idempotent."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS episodes (
                    id          TEXT PRIMARY KEY,
                    project     TEXT NOT NULL,
                    ts          TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    layer       TEXT NOT NULL,
                    source      TEXT NOT NULL,
                    confidence  REAL NOT NULL,
                    ttl         INTEGER,
                    provenance  TEXT NOT NULL DEFAULT '[]',
                    links       TEXT NOT NULL DEFAULT '[]',
                    tags        TEXT NOT NULL DEFAULT '[]',
                    metadata    TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            # Per-project index for read() and recent()
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_episodes_project "
                "ON episodes (project, ts)"
            )
            conn.commit()

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        prov = [ProvenanceEntry(**p) for p in json.loads(row["provenance"])]
        return Memory(
            id=row["id"],
            content=row["content"],
            layer=row["layer"],  # type: ignore[arg-type]
            source=row["source"],  # type: ignore[arg-type]
            ts=__import__("datetime").datetime.fromisoformat(row["ts"]),
            confidence=row["confidence"],
            ttl=row["ttl"],
            provenance=prov,
            links=json.loads(row["links"]),
            tags=json.loads(row["tags"]),
            metadata=json.loads(row["metadata"]),
        )

    @staticmethod
    def _append_provenance(memory: Memory) -> Memory:
        provenance = list(memory.provenance or [])
        has_hop = any(
            p.layer == "L3" and p.source == "hybrid" and p.id == memory.id
            for p in provenance
        )
        if not has_hop:
            provenance.append(
                ProvenanceEntry(layer="L3", source="hybrid", id=memory.id)
            )
        if len(provenance) > PROVENANCE_CHAIN_MAX:
            provenance = provenance[-PROVENANCE_CHAIN_MAX:]
        return memory.model_copy(update={"provenance": provenance})

    # --- public API ---

    def write(self, memory: Memory) -> None:
        """Upsert a Memory. Caller's tags are unioned with default_tags."""
        # Union tags
        existing_tags = set(memory.tags or [])
        for t in self.default_tags:
            if t not in existing_tags:
                existing_tags.add(t)
        stamped = self._append_provenance(memory).model_copy(
            update={"tags": sorted(existing_tags)}
        )

        row = (
            stamped.id,
            self.project,
            stamped.ts.isoformat(),
            stamped.content,
            stamped.layer,
            stamped.source,
            float(stamped.confidence),
            stamped.ttl,
            json.dumps(
                [p.model_dump() for p in (stamped.provenance or [])],
                ensure_ascii=False,
            ),
            json.dumps(stamped.links or [], ensure_ascii=False),
            json.dumps(stamped.tags or [], ensure_ascii=False),
            json.dumps(stamped.metadata or {}, ensure_ascii=False),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO episodes
                    (id, project, ts, content, layer, source, confidence,
                     ttl, provenance, links, tags, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
        logger.debug("HybridAdapter.write: id=%s project=%s", memory.id, self.project)

    def read(self, tag: str | None = None) -> list[Memory]:
        """Return entries for this project, optionally filtered by tag.

        ``tag`` is an exact-match filter (the tag must be in
        ``Memory.tags``). Pass ``None`` to get everything for the
        project.
        """
        with self._connect() as conn:
            if tag is None:
                rows = conn.execute(
                    "SELECT * FROM episodes WHERE project = ? ORDER BY ts",
                    (self.project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM episodes WHERE project = ? ORDER BY ts",
                    (self.project,),
                ).fetchall()
                rows = [r for r in rows if tag in json.loads(r["tags"])]
        return [self._row_to_memory(r) for r in rows]

    def search(self, query: str) -> list[Memory]:
        """Substring search (case-insensitive) within this project."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE project = ? ORDER BY ts",
                (self.project,),
            ).fetchall()
        if not query:
            return [self._row_to_memory(r) for r in rows]
        q = query.lower()
        out: list[Memory] = []
        for r in rows:
            if q in r["content"].lower():
                out.append(self._row_to_memory(r))
        return out

    def recent(self, n: int) -> list[Memory]:
        """Return the last ``n`` entries in insertion order (oldest first).

        ``n=0`` returns ``[]``. Negative ``n`` raises ``ValueError``.
        """
        if n < 0:
            raise ValueError(f"n must be >= 0, got {n}")
        if n == 0:
            return []
        all_entries = self.read()
        return all_entries[-n:]

    def delete(self, memory_id: str) -> bool:
        """Delete an entry by id. Returns True if removed, False if absent.

        Idempotent: deleting a non-existent id returns False.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM episodes WHERE id = ? AND project = ?",
                (memory_id, self.project),
            )
            conn.commit()
        return cur.rowcount > 0


__all__ = [
    "HybridAdapter",
    "DEFAULT_PROJECT",
    "DEFAULT_TAG",
    "ENV_STORAGE_DIR",
]
