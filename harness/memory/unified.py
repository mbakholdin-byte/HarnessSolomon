"""Unified Memory facade (Phase 1, Step 6).

The high-level entry point for the 4-layer memory system. Owns one
instance of each adapter and exposes a single API:

  - ``write(memory)``   — dual-write to primary + mirror layers
  - ``read(layer=X)``   — read from a specific layer
  - ``search(query)``   — search across all layers
  - ``recent(n)``       — recent entries (L3 hybrid)
  - ``delete(id)``      — remove from all layers

Dual-write policy (default):
  - primary = L2 (mem0) — semantic, the canonical retrieval store
  - mirrors = [L3, L4]  — episodic (Qdrant) + file (human-review)

L1 (hmem) is a special case: it is NOT in the default mirrors.
L1 is a hierarchical, prefix-coded knowledge base — entries
explicitly tagged layer="L1" go to hmem directly (and not to
mem0/hybrid/file). This matches the Solomon canon: hmem is a
separate, hand-curated store, not a mirror of semantic memory.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from harness.memory.adapters.file import FileAdapter
from harness.memory.adapters.hmem import HmemAdapter
from harness.memory.adapters.hybrid import HybridAdapter
from harness.memory.adapters.mem0 import Mem0Adapter
from harness.memory.schema import (
    ALL_LAYERS,
    Memory,
    MemoryLayer,
)

logger = logging.getLogger(__name__)


# === Constants ===

#: Default dual-write policy: primary = L2 (semantic, retrieval-friendly),
#: mirrors = L3 (episodic) + L4 (human-reviewable). L1 (hmem) is
#: intentionally NOT a default mirror.
DEFAULT_DUAL_WRITE_POLICY: dict[str, Any] = {
    "primary": "L2",
    "mirrors": ["L3", "L4"],
}

#: L1 is a hand-curated hierarchical store; entries tagged layer=L1
#: go there directly, not to the default L2+L3+L4 chain. This is the
#: Solomon canon (see CLAUDE.md §Inter-Agent Boundaries).
L1_OVERRIDE_TARGET: str = "L1"


# === Helpers ===

def _resolve_adapter_for_layer(
    layer: str,
    *,
    hmem: HmemAdapter,
    mem0: Mem0Adapter,
    hybrid: HybridAdapter,
    file: FileAdapter,
) -> HmemAdapter | Mem0Adapter | HybridAdapter | FileAdapter:
    """Return the adapter that owns the given layer."""
    if layer == "L1":
        return hmem
    if layer == "L2":
        return mem0
    if layer == "L2.5":
        # L2.5 is mempalace (KG). For now, fall back to mem0 (semantic).
        # TODO(Phase 2.1+ — separate track): add a real MemPalaceAdapter.
        # Phase 2.1 itself only touches sub-agents (cascade / background / namespacing),
        # not the mempalace KG adapter. See docs/roadmap.md §L2.5 track.
        return mem0
    if layer == "L3":
        return hybrid
    if layer == "L4":
        return file
    raise ValueError(f"unknown layer: {layer!r}")


# === Facade ===

class UnifiedMemory:
    """High-level facade over the four memory adapters.

    Args:
        hmem_dir:   Storage dir for the L1 hmem adapter.
        mem0_dir:   Storage dir for the L2 mem0 adapter.
        hybrid_dir: Storage dir for the L3 hybrid adapter.
        file_dir:   Storage dir for the L4 file adapter.
        dual_write_policy: ``{"primary": <layer>, "mirrors": [<layers>]}``.
                          ``primary`` MUST be one of L1/L2/L3/L4. The
                          mirrors list may be empty.
        agent_id:   Phase 2.1 — per-sub-agent namespace. Propagated to:
                      - ``HmemAdapter(agent=agent_id)``  (was hardcoded
                        ``"solomon"`` in Phase 1)
                      - ``Mem0Adapter(user_id=agent_id, collection=f"solomon-{agent_id}-memories")``
                      - ``HybridAdapter(project=agent_id, default_tags=[f"#agent/{agent_id}"])``
                      - ``FileAdapter(memory_dir=Path(file_dir) / agent_id)``
                    Defaults to ``"solomon"`` to preserve Phase 1 behaviour
                    for callers that don't know about sub-agents.
    """

    def __init__(
        self,
        hmem_dir: Path | str,
        mem0_dir: Path | str,
        hybrid_dir: Path | str,
        file_dir: Path | str,
        dual_write_policy: dict[str, Any] | None = None,
        *,
        agent_id: str = "solomon",
    ) -> None:
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError(f"agent_id must be a non-empty string, got {agent_id!r}")
        self.agent_id = agent_id
        self.hmem = HmemAdapter(memory_dir=Path(hmem_dir), agent=agent_id)
        self.mem0 = Mem0Adapter(
            storage_dir=Path(mem0_dir),
            user_id=agent_id,
            collection=f"solomon-{agent_id}-memories",
        )
        self.hybrid = HybridAdapter(
            storage_dir=Path(hybrid_dir),
            project=agent_id,
            default_tags=[f"#agent/{agent_id}"],
        )
        self.file = FileAdapter(memory_dir=Path(file_dir) / agent_id)

        self.policy = dict(dual_write_policy or DEFAULT_DUAL_WRITE_POLICY)
        primary = self.policy.get("primary")
        if primary not in ALL_LAYERS:
            raise ValueError(
                f"dual_write_policy.primary must be one of {ALL_LAYERS}, got {primary!r}"
            )
        mirrors = self.policy.get("mirrors", [])
        if not isinstance(mirrors, list):
            raise ValueError("dual_write_policy.mirrors must be a list")
        for m in mirrors:
            if m not in ALL_LAYERS:
                raise ValueError(
                    f"mirror layer must be one of {ALL_LAYERS}, got {m!r}"
                )

    # --- write ---

    def write(self, memory: Memory) -> None:
        """Dual-write a Memory to the primary + mirror layers.

        L1 is a special case: if ``memory.layer == "L1"``, we route
        directly to the hmem adapter regardless of the policy. The
        L1 store is a hand-curated knowledge base and doesn't
        participate in the L2/L3/L4 dual-write chain.

        Phase 2.1 — per-agent namespacing. Before dual-writing, we
        stamp the memory with this facade's ``agent_id`` so adapters
        that carry an explicit namespace (the L1 hmem agent, the L2
        mem0 user_id) see consistent data:

          - ``memory.metadata["agent_id"]`` — set to ``self.agent_id``
            when not already populated by the caller. We do NOT
            overwrite an explicit value.
          - ``memory.tags`` — append ``f"#agent/{self.agent_id}"`` if
            the tag isn't already present. No-op for the
            default ``"solomon"`` namespace so existing callers
            don't suddenly acquire a new tag.
          - ``memory.provenance`` — append a
            :class:`ProvenanceEntry(layer="L_meta", source="unified",
            id=agent_id)` so the audit trail records which facade
            stamped the memory.
        """
        # Stamp namespace metadata / tags / provenance BEFORE write.
        # We use object.__setattr__ via copy to keep the public
        # model immutable-feeling for callers (Pydantic v2 models
        # are mutable in practice; we just add to the fields).
        if memory.metadata.get("agent_id") is None:
            memory.metadata["agent_id"] = self.agent_id
        tag = f"#agent/{self.agent_id}"
        if tag not in memory.tags and self.agent_id != "solomon":
            memory.tags.append(tag)
        if not any(
            p.source == "unified" and p.id == self.agent_id
            for p in memory.provenance
        ):
            from harness.memory.schema import ProvenanceEntry  # local import
            memory.provenance.append(
                ProvenanceEntry(layer="L_meta", source="unified", id=self.agent_id)
            )

        if memory.layer == L1_OVERRIDE_TARGET:
            self._safe_write(self.hmem, memory)
            return

        primary_layer = self.policy["primary"]
        primary_adapter = _resolve_adapter_for_layer(
            primary_layer,
            hmem=self.hmem,
            mem0=self.mem0,
            hybrid=self.hybrid,
            file=self.file,
        )
        # Primary write MUST succeed.
        primary_adapter.write(memory)

        # Mirrors are best-effort. Failures are logged, not raised.
        for mirror_layer in self.policy.get("mirrors", []):
            mirror_adapter = _resolve_adapter_for_layer(
                mirror_layer,
                hmem=self.hmem,
                mem0=self.mem0,
                hybrid=self.hybrid,
                file=self.file,
            )
            self._safe_write(mirror_adapter, memory)

    @staticmethod
    def _safe_write(adapter: Any, memory: Memory) -> None:
        """Mirror write — catch and log failures so the primary isn't lost."""
        try:
            adapter.write(memory)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "UnifiedMemory: mirror write to %s failed for id=%s: %s",
                type(adapter).__name__, memory.id, exc,
            )

    # --- read ---

    def read(self, layer: str | None = None) -> list[Memory]:
        """Read entries.

        ``layer`` (e.g. ``"L2"``) returns only that adapter's
        entries. ``None`` reads all four layers and deduplicates
        by ``Memory.id`` (keeping the first occurrence, which
        follows the policy: primary first, then mirrors in order).
        """
        if layer is not None:
            adapter = _resolve_adapter_for_layer(
                layer,
                hmem=self.hmem,
                mem0=self.mem0,
                hybrid=self.hybrid,
                file=self.file,
            )
            if hasattr(adapter, "read"):
                return list(adapter.read())
            return []

        # All four, in policy order, dedup by id
        primary_layer = self.policy["primary"]
        order: list[str] = [primary_layer] + [
            m for m in self.policy.get("mirrors", []) if m != primary_layer
        ]
        # Plus L1 if there's anything there
        seen: set[str] = set()
        out: list[Memory] = []
        for lyr in order + [L1_OVERRIDE_TARGET]:
            adapter = _resolve_adapter_for_layer(
                lyr,
                hmem=self.hmem,
                mem0=self.mem0,
                hybrid=self.hybrid,
                file=self.file,
            )
            if not hasattr(adapter, "read"):
                continue
            for entry in adapter.read():
                if entry.id in seen:
                    continue
                seen.add(entry.id)
                out.append(entry)
        return out

    # --- search ---

    def search(self, query: str) -> list[Memory]:
        """Substring search across all four layers, deduped by id.

        Order: primary first, then mirrors, then L1. Empty ``query``
        returns everything (deduped).

        Adapters return different shapes from ``search()``:
          - hmem, hybrid, file → ``list[Memory]``
          - mem0                → ``list[tuple[Memory, float]]``
        We normalise to ``list[Memory]`` here.
        """
        primary_layer = self.policy["primary"]
        order: list[Any] = [primary_layer] + [
            m for m in self.policy.get("mirrors", []) if m != primary_layer
        ]
        adapters: list[Any] = [
            _resolve_adapter_for_layer(
                lyr,
                hmem=self.hmem,
                mem0=self.mem0,
                hybrid=self.hybrid,
                file=self.file,
            )
            for lyr in order + [L1_OVERRIDE_TARGET]
        ]
        seen: set[str] = set()
        out: list[Memory] = []
        for adapter in adapters:
            if not hasattr(adapter, "search"):
                continue
            for item in adapter.search(query):
                mem = item[0] if isinstance(item, tuple) else item
                if mem.id in seen:
                    continue
                seen.add(mem.id)
                out.append(mem)
        return out

    # --- recent ---

    def recent(self, n: int) -> list[Memory]:
        """Tail-insert-order entries from L3 (hybrid episodic)."""
        return self.hybrid.recent(n)

    # --- delete ---

    def delete(self, memory_id: str) -> int:
        """Delete an entry from every layer that has it.

        Returns the number of layers it was removed from. The hmem
        adapter has no ``delete()`` (it's an append-only journal);
        we skip it explicitly here. Other adapters without
        ``delete()`` are also skipped.
        """
        count = 0
        for adapter in (self.mem0, self.hybrid, self.file):
            if hasattr(adapter, "delete") and adapter.delete(memory_id):
                count += 1
        return count


__all__ = [
    "UnifiedMemory",
    "DEFAULT_DUAL_WRITE_POLICY",
    "L1_OVERRIDE_TARGET",
]
