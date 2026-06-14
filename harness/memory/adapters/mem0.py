"""mem0 adapter — L2 semantic memory (Phase 1, Step 3).

The mem0 (memory-zero) layer stores semantic facts / preferences
keyed by ``user_id``. Solomon uses ``user_id="solomon"`` and
``collection="solomon-memories"``.

For production, this maps onto either:
  - the ``mcp__mem0__*`` MCP server (preferred), or
  - a direct Qdrant collection (legacy ``solomon-memories``).

This adapter provides a file-backed fallback (one JSONL per user)
that conforms to the same ``Mem0Adapter`` API. The on-disk format
is the contract, so the MCP server and the file adapter are
interchangeable.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterator

from harness.memory.schema import (
    PROVENANCE_CHAIN_MAX,
    Memory,
    ProvenanceEntry,
)

logger = logging.getLogger(__name__)


# === Constants ===

#: Env var to override the default storage dir.
ENV_STORAGE_DIR: str = "SOLOMON_MEM0_DIR"


# === Helpers ===

def _default_dir() -> Path:
    """Default storage dir for the file-backed fallback.

    Priority: ``$SOLOMON_MEM0_DIR`` → system temp ``/solomon-mem0``
    (Windows) / ``~/.solomon/mem0`` (Unix) → ``./data/mem0``.
    """
    env = os.environ.get(ENV_STORAGE_DIR, "").strip()
    if env:
        return Path(env)
    home = Path(os.path.expanduser("~"))
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "solomon-mem0"
    return home / ".solomon" / "mem0"


def _safe_filename(user_id: str) -> str:
    """Sanitise user_id into a safe filename stem."""
    safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    if not safe or safe in (".", ".."):
        safe = "default"
    return safe


# === Adapter ===

class Mem0Adapter:
    """File-backed L2 semantic memory adapter.

    Args:
        storage_dir: Directory holding one JSONL file per user
                     (``<user_id>.jsonl``). Created if missing.
        user_id:     Scoping key — only this user's entries are
                     visible to read(). Required, non-empty.
        collection:  Logical collection name (Qdrant-style). Used in
                     metadata for round-tripping; the file backend
                     ignores it.
    """

    def __init__(
        self,
        storage_dir: Path | str | None = None,
        user_id: str = "solomon",
        collection: str = "solomon-memories",
    ) -> None:
        if not user_id or not isinstance(user_id, str):
            raise ValueError(f"user_id must be a non-empty string, got {user_id!r}")
        if storage_dir is None:
            self.storage_dir = _default_dir()
        else:
            self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.user_id = user_id
        self.collection = collection
        self._file = self.storage_dir / f"{_safe_filename(user_id)}.jsonl"
        logger.debug(
            "Mem0Adapter: user_id=%s collection=%s file=%s",
            user_id, collection, self._file,
        )

    # --- internal ---

    def _read_all(self) -> list[Memory]:
        if not self._file.exists():
            return []
        out: list[Memory] = []
        with self._file.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(Memory.from_jsonl(raw))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Mem0Adapter: skipping malformed line in %s: %s",
                        self._file, exc,
                    )
        return out

    def _atomic_write(self, entries: list[Memory]) -> None:
        """Rewrite the whole file atomically (tmp + rename)."""
        tmp = self._file.with_suffix(self._file.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(e.to_jsonl())
                f.write("\n")
        tmp.replace(self._file)

    def _append_provenance(self, memory: Memory) -> Memory:
        """Return a copy with an L2/mem0 hop appended (FIFO-capped)."""
        provenance = list(memory.provenance or [])
        has_hop = any(
            p.layer == "L2" and p.source == "mem0" and p.id == memory.id
            for p in provenance
        )
        if not has_hop:
            provenance.append(
                ProvenanceEntry(layer="L2", source="mem0", id=memory.id)
            )
        if len(provenance) > PROVENANCE_CHAIN_MAX:
            provenance = provenance[-PROVENANCE_CHAIN_MAX:]
        return memory.model_copy(update={"provenance": provenance})

    # --- public API ---

    def write(self, memory: Memory) -> None:
        """Upsert a Memory keyed by id (mem0 semantics).

        If an entry with the same id already exists, it is replaced.
        """
        stamped = self._append_provenance(memory)
        existing = self._read_all()
        # Replace in place; otherwise append
        replaced = False
        for i, e in enumerate(existing):
            if e.id == stamped.id:
                existing[i] = stamped
                replaced = True
                break
        if not replaced:
            existing.append(stamped)
        self._atomic_write(existing)
        logger.debug("Mem0Adapter.write: id=%s replaced=%s", memory.id, replaced)

    def read(self) -> list[Memory]:
        """Return all entries for this user."""
        return self._read_all()

    def delete(self, memory_id: str) -> bool:
        """Delete an entry by id. Returns True if removed, False if absent."""
        existing = self._read_all()
        kept = [e for e in existing if e.id != memory_id]
        if len(kept) == len(existing):
            return False
        self._atomic_write(kept)
        return True

    def search(self, query: str) -> list[tuple[Memory, float]]:
        """Substring search (case-insensitive) with a fuzzy score.

        Score = length-weighted similarity: ``matched_chars / content_len``.
        This is a simple proxy for semantic similarity — fine for
        file-backed fallback. The real Qdrant-backed path uses
        vector cosine. Score is in [0, 1].
        """
        entries = self._read_all()
        if not query:
            return [(e, 1.0) for e in entries]
        q = query.lower()
        results: list[tuple[Memory, float]] = []
        for e in entries:
            content_lower = e.content.lower()
            if q in content_lower:
                # Substring hits score high; longer content with the
                # match in fewer characters → higher score
                score = len(q) / max(len(content_lower), len(q))
                results.append((e, min(score, 1.0)))
        # Sort by score desc, then by id for stable order
        results.sort(key=lambda r: (-r[1], r[0].id))
        return results

    def __iter__(self) -> Iterator[Memory]:
        """Iterate over all entries for this user."""
        return iter(self._read_all())


__all__ = [
    "Mem0Adapter",
    "ENV_STORAGE_DIR",
]
