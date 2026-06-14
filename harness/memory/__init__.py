"""Solomon Harness — 4-layer unified memory (Phase 1).

Sub-packages:
  - ``schema``:    canonical ``Memory`` Pydantic model + layer/source
                  constants. Import this first.
  - ``adapters``: 4 storage adapters (hmem, mem0, hybrid, file) that
                  translate between ``Memory`` and their native format.
  - ``unified``:  high-level facade (``UnifiedMemory``) over all four
                  adapters, with dual-write and search.
  - ``retrieval``: hybrid (BM25+vector) → rerank → assembly pipeline.
"""
from harness.memory.retrieval import (
    BM25Retriever,
    ContextAssembler,
    DEFAULT_CANDIDATE_K,
    DEFAULT_TOP_K,
    IdentityReranker,
    RetrievalPipeline,
)
from harness.memory.schema import (
    ALL_LAYERS,
    PROVENANCE_CHAIN_MAX,
    Memory,
    MemoryLayer,
    MemorySource,
    ProvenanceEntry,
)
from harness.memory.unified import (
    DEFAULT_DUAL_WRITE_POLICY,
    L1_OVERRIDE_TARGET,
    UnifiedMemory,
)

__all__ = [
    "ALL_LAYERS",
    "PROVENANCE_CHAIN_MAX",
    "Memory",
    "MemoryLayer",
    "MemorySource",
    "ProvenanceEntry",
    "UnifiedMemory",
    "DEFAULT_DUAL_WRITE_POLICY",
    "L1_OVERRIDE_TARGET",
    "BM25Retriever",
    "ContextAssembler",
    "IdentityReranker",
    "RetrievalPipeline",
    "DEFAULT_CANDIDATE_K",
    "DEFAULT_TOP_K",
]
