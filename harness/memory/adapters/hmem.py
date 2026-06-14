"""hmem adapter — L1 hierarchical memory (Phase 1, Step 2).

The hmem (hierarchical memory) layer stores structured knowledge
with a single-letter prefix that classifies the entry:

  P=Project, L=Lesson, T=Task, E=Error, D=Decision, M=Milestone,
  S=Skill, N=Navigator, H=Human, R=Rule, O=Original

Each entry is a ``Memory`` from the unified schema. The hmem adapter
translates between the unified ``Memory`` and the on-disk JSONL
format (one ``.hmem`` file per agent, one JSON object per line).

For production, the ``mcp__hmem__*`` MCP server can also write to the
same files — the file format is the contract. This adapter is the
Python-side equivalent, useful for unit tests, offline use, and
when the MCP server is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from harness.memory.schema import (
    PROVENANCE_CHAIN_MAX,
    Memory,
    ProvenanceEntry,
)

logger = logging.getLogger(__name__)


# === Constants ===

#: Valid hmem prefix codes. ``Memory.metadata["hmem_prefix"]`` must be
#: one of these (or omitted — see ``_DEFAULT_PREFIX``).
VALID_PREFIXES: frozenset[str] = frozenset(
    {"P", "L", "T", "E", "D", "M", "S", "N", "H", "R", "O"}
)

#: Default prefix when an L1 entry omits ``metadata.hmem_prefix``.
#: "D" (Decision) is the safest default — it is the most common
#: entry type and the hmem READ prompt expects D-prefixed entries
#: first.
_DEFAULT_PREFIX: str = "D"

#: Env var to override the default memory dir.
ENV_MEMORY_DIR: str = "SOLOMON_HMEM_DIR"


# === Helpers ===

def _default_dir() -> Path:
    """Return the default hmem memory dir.

    Priority: ``$SOLOMON_HMEM_DIR`` → ``$USERPROFILE/SolomonHmem``
    (Windows) / ``$HOME/.solomon/hmem`` (Unix) → cwd ``./data/hmem``.
    """
    env = os.environ.get(ENV_MEMORY_DIR, "").strip()
    if env:
        return Path(env)
    home = Path(os.path.expanduser("~"))
    if os.name == "nt":
        return home / "SolomonHmem"
    return home / ".solomon" / "hmem"


def _safe_filename(agent: str) -> str:
    """Sanitise agent name into a safe filename stem (no slashes, no ..)."""
    safe = "".join(c for c in agent if c.isalnum() or c in ("-", "_"))
    if not safe or safe in (".", ".."):
        safe = "default"
    return safe


# === Adapter ===

class HmemAdapter:
    """File-backed L1 hierarchical memory adapter.

    Args:
        memory_dir: Directory holding ``<agent>.hmem`` JSONL files.
                    Created if missing.
        agent:      Agent namespace. Each agent gets its own file
                    (``solomon.hmem``, ``alex.hmem``, etc.).

    The on-disk format is JSONL: one ``Memory.model_dump_json()`` per
    line. The first line is the most recently written; we always
    append.
    """

    def __init__(
        self,
        memory_dir: Path | str | None = None,
        agent: str = "solomon",
    ) -> None:
        if memory_dir is None:
            self.memory_dir = _default_dir()
        else:
            self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.agent = agent
        self._file = self.memory_dir / f"{_safe_filename(agent)}.hmem"
        logger.debug("HmemAdapter: agent=%s file=%s", agent, self._file)

    # --- internal ---

    def _append_line(self, line: str) -> None:
        """Append a single line to the .hmem file. Thread-unsafe by design."""
        with self._file.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")

    def _read_all_lines(self) -> list[Memory]:
        """Load every entry from the .hmem file. Returns [] if missing."""
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
                except Exception as exc:  # noqa: BLE001 — surface but don't kill
                    logger.warning(
                        "HmemAdapter: skipping malformed line in %s: %s",
                        self._file, exc,
                    )
        return out

    @staticmethod
    def _prefix_for(memory: Memory) -> str:
        """Resolve the hmem prefix for a Memory.

        Order of precedence:
          1. ``memory.metadata["hmem_prefix"]`` (must be in VALID_PREFIXES)
          2. Default ``_DEFAULT_PREFIX`` ("D") for L1 entries
          3. None for non-L1 layers (hmem is L1-only)
        """
        prefix = memory.metadata.get("hmem_prefix") if memory.metadata else None
        if prefix and prefix in VALID_PREFIXES:
            return prefix
        if memory.layer == "L1":
            return _DEFAULT_PREFIX
        # Non-L1 layers don't really belong in hmem, but if someone
        # insists we still store them — under a generic "O" (Original)
        # prefix so the file stays valid.
        return "O"

    # --- public API ---

    def write(self, memory: Memory) -> None:
        """Append a Memory to the agent's .hmem file.

        Provenance: if the entry doesn't already have an L1/hmem hop,
        we append one at the end (FIFO, capped at PROVENANCE_CHAIN_MAX).
        """
        prefix = self._prefix_for(memory)
        # Build the on-disk copy with the prefix baked into metadata
        # (so we can re-read it later without context loss).
        meta = dict(memory.metadata or {})
        meta.setdefault("hmem_prefix", prefix)

        # Append provenance hop if missing
        provenance = list(memory.provenance or [])
        has_hmem_hop = any(
            p.layer == "L1" and p.source == "hmem" and p.id == memory.id
            for p in provenance
        )
        if not has_hmem_hop:
            provenance.append(
                ProvenanceEntry(layer="L1", source="hmem", id=memory.id)
            )
        # FIFO cap
        if len(provenance) > PROVENANCE_CHAIN_MAX:
            provenance = provenance[-PROVENANCE_CHAIN_MAX:]

        # Re-emit the Memory with the updated metadata + provenance
        stored = memory.model_copy(
            update={"metadata": meta, "provenance": provenance}
        )
        self._append_line(stored.to_jsonl())
        logger.debug("HmemAdapter.write: id=%s prefix=%s", memory.id, prefix)

    def read(self, prefix: str | None = None) -> list[Memory]:
        """Return all entries; optionally filtered by hmem prefix.

        ``prefix`` is the single-letter hmem code ("D", "L", "E", ...).
        ``None`` returns everything regardless of prefix.
        """
        entries = self._read_all_lines()
        if prefix is None:
            return entries
        return [
            e for e in entries
            if (e.metadata or {}).get("hmem_prefix") == prefix
        ]

    def search(self, query: str) -> list[Memory]:
        """Substring search across all entries (case-insensitive).

        Empty ``query`` returns all entries (useful for "list everything").
        """
        entries = self._read_all_lines()
        if not query:
            return entries
        q = query.lower()
        return [e for e in entries if q in e.content.lower()]


__all__ = [
    "HmemAdapter",
    "VALID_PREFIXES",
    "ENV_MEMORY_DIR",
]
