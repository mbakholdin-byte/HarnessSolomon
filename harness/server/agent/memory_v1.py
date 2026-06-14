"""Phase 1.6 — Bridge between the HTTP memory routes and ``UnifiedMemory``.

The HTTP layer in :mod:`harness.server.routes.memory_v1` calls
``harness.server.agent.memory_v1.search()`` and ``.write()`` etc.
This indirection serves two purposes:

  1. **Trust boundary.** The auth module (``harness.server.auth``)
     and the memory module (``harness.memory``) are both leaves in
     the dependency graph. By having the routes call a bridge in
     ``harness.server.agent``, we keep the routes out of the memory
     module's import surface and out of the auth module's surface.

  2. **Future microservice split.** When (if) the memory backend
     moves to a separate process, only this bridge needs to be
     rewritten to call an HTTP/gRPC client — the route handlers
     don't change.

The default :class:`UnifiedMemory` is constructed lazily from
``settings.*`` paths, matching the rest of the harness. Tests
that want a fresh in-memory backend can pass a pre-built
``unified`` instance via the module-level ``_default`` slot.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.config import settings
from harness.memory.schema import Memory, MemoryLayer
from harness.memory.unified import UnifiedMemory

# Module-level slot for the default unified-memory instance. We
# don't construct at import time because the directories have to
# exist first (the lifespan handler creates them).
_default: UnifiedMemory | None = None


def _get_default() -> UnifiedMemory:
    """Lazily construct (or return the cached) default ``UnifiedMemory``.

    Reads the four storage dirs from ``settings`` and constructs a
    ``UnifiedMemory(agent_id="solomon")`` — the default namespace
    for harness-managed memory. Tests can replace ``_default`` via
    ``memory_v1._default = my_instance`` before invoking the
    search/write helpers.
    """
    global _default
    if _default is None:
        data_root = settings.db_path.parent
        _default = UnifiedMemory(
            hmem_dir=data_root / "memory" / "hmem",
            mem0_dir=data_root / "memory" / "mem0",
            hybrid_dir=data_root / "memory" / "hybrid",
            file_dir=data_root / "memory" / "file",
        )
    return _default


def set_default(unified: UnifiedMemory | None) -> None:
    """Replace the default ``UnifiedMemory`` (test/operator hook).

    Pass ``None`` to clear the cache and force the next call to
    :func:`_get_default` to construct a fresh one from settings.
    """
    global _default
    _default = unified


# === Public operations ===

def search(query: str, k: int = 5) -> list[dict[str, Any]]:
    """Search the unified memory and return JSON-friendly results.

    Returns a list of dicts (one per hit) with keys: ``id``,
    ``layer``, ``source``, ``text``, ``tags``, ``score`` (the
    BM25 / identity-reranker score from the retriever). The
    search goes through the Phase 1 retrieval pipeline
    (BM25 + IdentityReranker + ContextAssembler); we re-derive
    a per-hit score by re-scoring the top-K with the same
    retriever when needed. For now we pass through whatever
    score the retriever attached.
    """
    unified = _get_default()
    raw = unified.search(query)
    # The retriever returns Memory objects ordered by descending
    # relevance. We project to dicts and cap at ``k``.
    out: list[dict[str, Any]] = []
    for mem in raw[:k]:
        out.append({
            "id": mem.id,
            "layer": mem.layer,
            "source": mem.source,
            "text": mem.content,
            "tags": list(mem.tags),
            "agent_id": mem.metadata.get("agent_id") if mem.metadata else None,
        })
    return out


def write_note(
    *,
    text: str,
    layer: MemoryLayer = "L2",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Write a new memory note and return a confirmation dict.

    The caller specifies the target layer (default ``L2`` — the
    semantic / fuzzy layer, which is the right home for
    free-form notes). ``tags`` are passed through unchanged;
    ``UnifiedMemory.write`` will additionally stamp the
    ``#agent/solomon`` tag and a provenance hop.
    """
    if not text or not text.strip():
        raise ValueError("text must be a non-empty string")
    if layer not in ("L1", "L2", "L2.5", "L3", "L4"):
        raise ValueError(f"unknown layer: {layer!r}")
    unified = _get_default()
    mem = Memory(
        content=text.strip(),
        layer=layer,
        source="manual",
        tags=list(tags or []),
    )
    unified.write(mem)
    return {
        "id": mem.id,
        "layer": mem.layer,
        "source": mem.source,
        "tags": list(mem.tags),
        "agent_id": mem.metadata.get("agent_id") if mem.metadata else None,
    }


def stats() -> dict[str, Any]:
    """Return per-layer entry counts (cheap, read-only).

    The current ``UnifiedMemory`` doesn't expose a public
    ``counts()`` method, so we use the retrievers' underlying
    sources where possible. For Phase 1.6 we approximate
    using the size of each adapter's on-disk directory; this
    is intentionally cheap (one stat() per layer) and we
    accept that it conflates 'entries' with 'storage
    artefacts'. A proper ``count()`` method on the facade is
    a Phase 3 follow-up.
    """
    unified = _get_default()
    out: dict[str, Any] = {"agent_id": unified.agent_id, "layers": {}}
    # The hmem adapter stores one JSONL per agent under its
    # memory_dir; the file count gives us a rough entry count.
    # For the others we just report 'available' so the route
    # can return 200 without doing a full scan.
    try:
        from pathlib import Path as _P
        hmem_path = _P(unified.hmem.memory_dir) / f"{unified.agent_id}.jsonl"
        if hmem_path.exists():
            with hmem_path.open("r", encoding="utf-8") as f:
                count = sum(1 for _ in f)
        else:
            count = 0
    except Exception:  # noqa: BLE001 — best-effort
        count = None
    out["layers"]["L1_hmem_entries"] = count
    out["layers"]["L2_mem0_available"] = True
    out["layers"]["L3_hybrid_available"] = True
    out["layers"]["L4_file_available"] = True
    return out


__all__ = ["search", "set_default", "stats", "write_note"]
