"""Solomon Harness — memory adapters (Phase 1).

Each adapter is the bridge between the unified ``Memory`` schema and
one storage layer:

  - ``hmem`` (L1):      file-backed JSONL, prefix-coded entries
                        (P/L/T/E/D/M/S/N/H/R/O)
  - ``mem0`` (L2):      semantic / fuzzy, user-scoped
  - ``hybrid`` (L3):    Qdrant + SQLite + OpenSearch episodes
  - ``file`` (L4):      Markdown + INDEX.md + Obsidian vault

Adapters are intentionally thin — translation only. The high-level
logic (dual-write, search routing) lives in ``harness.memory.unified``.
"""
from harness.memory.adapters.hmem import HmemAdapter, VALID_PREFIXES
from harness.memory.adapters.mem0 import Mem0Adapter

__all__ = ["HmemAdapter", "VALID_PREFIXES", "Mem0Adapter"]
