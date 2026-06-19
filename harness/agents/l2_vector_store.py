"""L2 vector store — embeddings backend for scratchpad L2 notes (Phase 3 v1.3.0).

Phase 3 v1.3.0 introduces the "Select" strategy from the Anthropic
context-engineering playbook: dense+BM25 hybrid retrieval over the
L2 archive of the scratchpad. This module provides the dense half
of that equation (the BM25 half is in :mod:`harness.agents.l2_retriever`).

The vector store has two backends:

  * **Qdrant** (primary) — separate collection ``scratchpad_l2`` on
    a user-configured Qdrant server. Production-grade, scales to
    millions of notes, supports payload filters (session_id,
    agent_id) for free. Requires ``qdrant-client>=1.7`` from the
    ``[memory]`` extra (already declared in ``pyproject.toml``).

  * **SQLite** (fallback) — vectors stored as BLOB in the existing
    ``scratchpad_notes`` table. Zero new dependencies, works
    offline, ideal for development and small corpora (<10K notes).
    On every query, the in-memory numpy matrix is rebuilt from the
    SQLite rows; the cost is linear in the corpus size.

The :func:`make_l2_store` factory picks the backend based on
``settings.scratchpad_l2_qdrant_url``: Qdrant if the URL is set
AND the server is reachable, SQLite otherwise. The Qdrant probe
is best-effort with a short timeout — a dead Qdrant is treated
the same as "not configured" and we fall through to SQLite.

**Trust boundary:** this module imports ``qdrant_client`` lazily
(inside :class:`QdrantL2Store.__init__`) so the harness can run
without the ``[memory]`` extra installed. Tests that don't need
Qdrant get full coverage from the SQLite path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiosqlite
import numpy as np

from harness.hooks.runner import safe_fire  # Phase 4.13A v1.23.0: OnMemoryWrite hook
from .scratchpad import NoteLevel

logger = logging.getLogger(__name__)


# === Phase 4.13A v1.23.0: OnMemoryWrite helper ============================
#
# Both L2 store backends share a single helper to fire the
# ``OnMemoryWrite`` event. The payload follows the schema in
# ``harness.hooks.schemas.OnMemoryWritePayload`` (``layer``, ``key_hash``,
# ``scope``, ``size_bytes``) plus the Phase 4.13A-specific fields
# (``session_id``, ``agent_id``, ``key``, ``value_size``, ``timestamp``).
# The Pydantic schema is ``extra="ignore"`` so the additional fields
# survive advisory validation without raising.
#
# Hot-path: ``safe_fire`` is non-blocking in an asyncio context (it is
# scheduled via ``loop.create_task``) and swallow-and-log outside a
# running loop, so the L2 upsert path is never blocked by the hook.

def _fire_on_memory_write(
    *,
    note_id: int,
    vector: list[float],
    payload: dict[str, Any],
) -> None:
    """Fire ``OnMemoryWrite`` for an L2 store upsert (best-effort).

    Args:
        note_id:  The numeric note id (Qdrant point id / SQLite row id).
        vector:   The persisted vector — used ONLY to compute
            ``value_size`` (``len(vector)``); the raw vector is NEVER
            placed in the payload (PII / size risk).
        payload:  The Qdrant payload / SQLite ``embedding_payload``
            dict. ``session_id`` and ``agent_id`` are surfaced if
            present, but the raw dict itself is NOT placed in the
            event payload — only a short ``key_hash`` is emitted.
    """
    key = str(note_id)
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    session_id = str(payload.get("session_id", "")) if isinstance(payload, dict) else ""
    agent_id = str(payload.get("agent_id", "")) if isinstance(payload, dict) else ""
    size_bytes = len(vector) * 4  # float32 → 4 bytes per element
    event_payload = {
        # Schema-required fields (see OnMemoryWritePayload).
        "layer": "L2",
        "key_hash": key_hash,
        "scope": agent_id or session_id or "scratchpad",
        "size_bytes": size_bytes,
        # Phase 4.13A v1.23.0 fields (extra on the schema, ignored by
        # advisory validation but surfaced for hook consumers).
        "session_id": session_id,
        "agent_id": agent_id,
        # ``note_id`` (numeric scratchpad row id) — NOT a raw secret.
        # The schema explicitly forbids a raw ``key`` field (PII
        # regression guard); note_id is a benign identifier.
        "note_id": int(note_id),
        "value_size": size_bytes,
        "timestamp": time.time(),
    }
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            safe_fire("OnMemoryWrite", payload=event_payload)
        )
    except RuntimeError:
        # No running event loop (tests, CLI, REPL). Swallow silently —
        # the upsert itself MUST proceed regardless of hook availability.
        pass
    except Exception:  # noqa: BLE001 — hot path must never break on hook
        logger.debug(
            "OnMemoryWrite fire failed for note_id=%s", note_id, exc_info=True,
        )


# === Protocol ===

@runtime_checkable
class L2VectorStore(Protocol):
    """Dense-vector backend for L2 scratchpad notes.

    The protocol is intentionally narrow: only the operations the
    L2 retrieval pipeline actually needs (upsert / search / delete /
    count). Payload filters happen at the Qdrant level for the
    primary backend and at the SQLite level for the fallback.

    All methods are ``async`` so the harness's existing asyncio
    call sites (notably the L2 retriever) don't need a special
    sync-over-async bridge for the SQLite case.
    """

    async def upsert(
        self,
        note_id: int,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        """Insert or update the vector for ``note_id`` with metadata.

        ``payload`` is the JSON-serialisable dict stored alongside
        the vector (``session_id``, ``agent_id``, ``level``,
        ``created_at``, ``tags``). The Qdrant backend exposes it
        as a filter; the SQLite backend denormalises it into a
        JSON column.
        """
        ...

    async def search(
        self,
        query_vector: list[float],
        top_k: int,
        *,
        filter: dict[str, Any] | None = None,
    ) -> list[tuple[int, float, dict[str, Any]]]:
        """Return top-k ``(note_id, score, payload)`` by cosine similarity.

        ``filter`` is an optional payload predicate (Qdrant native
        syntax; the SQLite backend interprets a small subset — see
        :class:`SqliteL2Store` for the supported keys). Returns an
        empty list when the store is empty.
        """
        ...

    async def delete(self, note_id: int) -> bool:
        """Remove a note's vector. Returns True if a row was removed."""
        ...

    async def count(self) -> int:
        """Return the total number of vectors in the store."""
        ...


# === SqliteL2Store (fallback) ===

class SqliteL2Store:
    """SQLite-backed dense vector store (Phase 3 v1.3.0 fallback).

    Vectors are stored as a BLOB column (``embedding``) on the
    existing ``scratchpad_notes`` table. The ``embedding_payload``
    column carries the JSON-serialisable metadata. The store
    piggybacks on the scratchpad DB to avoid a second connection
    pool and to keep the L2 archive's lifecycle aligned with the
    notes themselves (cascade delete is automatic since they live
    on the same row).

    Query path: load all vectors into a numpy matrix, compute
    cosine similarity, return the top-k. For a 10K-note archive
    on a modern laptop this takes <50ms; for an offline dev
    workflow that's well below the LLM-curator round-trip cost.
    For larger archives the operator should set
    ``scratchpad_l2_qdrant_url`` to point at a real Qdrant.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def _ensure_column(self) -> None:
        """Add the ``embedding`` and ``embedding_payload`` columns if
        they don't exist yet. Idempotent: a SELECT-then-ALTER dance
        that survives both fresh and migrated DBs.
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            # Discover existing columns.
            await db.execute(
                "SELECT name FROM pragma_table_info('scratchpad_notes')"
            )
            # Use a fresh cursor for the actual introspection.
            cur = await db.execute(
                "SELECT name FROM pragma_table_info('scratchpad_notes')"
            )
            cols = {row[0] for row in await cur.fetchall()}
            if "embedding" not in cols:
                await db.execute(
                    "ALTER TABLE scratchpad_notes ADD COLUMN embedding BLOB"
                )
            if "embedding_payload" not in cols:
                await db.execute(
                    "ALTER TABLE scratchpad_notes ADD COLUMN embedding_payload TEXT"
                )
            await db.commit()

    async def upsert(
        self,
        note_id: int,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        await self._ensure_column()
        arr = np.asarray(vector, dtype=np.float32)
        # Re-normalise defensively (L2-normalised vectors are the
        # contract from OnnxEmbedder; the safety net is for callers
        # that hand-roll a vector and forget to normalise).
        n = np.linalg.norm(arr)
        if n > 0:
            arr = arr / n
        blob = arr.tobytes()
        payload_json = json.dumps(payload, ensure_ascii=False)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute(
                "UPDATE scratchpad_notes SET embedding = ?, "
                "embedding_payload = ? WHERE id = ?",
                (blob, payload_json, int(note_id)),
            )
            await db.commit()
        # Phase 4.13A v1.23.0: OnMemoryWrite hook (hot-path safe_fire).
        _fire_on_memory_write(
            note_id=note_id, vector=vector, payload=payload,
        )

    async def search(
        self,
        query_vector: list[float],
        top_k: int,
        *,
        filter: dict[str, Any] | None = None,
    ) -> list[tuple[int, float, dict[str, Any]]]:
        await self._ensure_column()
        top_k = max(1, int(top_k))
        q = np.asarray(query_vector, dtype=np.float32)
        n = np.linalg.norm(q)
        if n > 0:
            q = q / n
        # Pull all rows that have a non-empty embedding. We do the
        # filter in Python to keep the SQL portable across the
        # fallback and the future Qdrant schema. For larger corpora
        # the operator should switch to Qdrant.
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            cur = await db.execute(
                "SELECT id, embedding, embedding_payload "
                "FROM scratchpad_notes WHERE embedding IS NOT NULL"
            )
            rows = await cur.fetchall()
        if not rows:
            return []
        ids: list[int] = []
        vectors: list[np.ndarray] = []
        payloads: list[dict[str, Any]] = []
        for row in rows:
            blob = row["embedding"]
            if not blob:
                continue
            vec = np.frombuffer(blob, dtype=np.float32)
            nrm = np.linalg.norm(vec)
            if nrm > 0:
                vec = vec / nrm
            payload_raw = row["embedding_payload"]
            try:
                payload = (
                    json.loads(payload_raw) if payload_raw else {}
                )
            except json.JSONDecodeError:
                payload = {}
            if filter and not _payload_matches(payload, filter):
                continue
            ids.append(int(row["id"]))
            vectors.append(vec)
            payloads.append(payload)
        if not vectors:
            return []
        matrix = np.stack(vectors).astype(np.float32)
        scores = matrix @ q   # cosine since both sides L2-normalised
        # Top-k indices, descending.
        order = np.argsort(-scores)[:top_k]
        return [
            (ids[i], float(scores[i]), payloads[i])
            for i in order
            if float(scores[i]) > 0   # negative cosine = dissimilar
        ]

    async def delete(self, note_id: int) -> bool:
        await self._ensure_column()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            cur = await db.execute(
                "UPDATE scratchpad_notes SET embedding = NULL, "
                "embedding_payload = NULL WHERE id = ?",
                (int(note_id),),
            )
            await db.commit()
            return cur.rowcount > 0

    async def count(self) -> int:
        await self._ensure_column()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM scratchpad_notes "
                "WHERE embedding IS NOT NULL"
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0


def _payload_matches(
    payload: dict[str, Any], filter: dict[str, Any],
) -> bool:
    """Tiny filter language for the SQLite backend.

    Supports equality on top-level keys. The Qdrant backend passes
    through to Qdrant's native filter DSL; the SQLite backend only
    needs the few fields the harness actually filters on
    (``session_id``, ``agent_id``) for cross-session isolation.
    """
    for k, expected in filter.items():
        if payload.get(k) != expected:
            return False
    return True


# === QdrantL2Store (primary, optional) ===

class QdrantL2Store:
    """Qdrant-backed dense vector store (Phase 3 v1.3.0 primary).

    The collection is created on first use with cosine distance
    and a configurable vector size (default 384, matching
    ``multilingual-e5-small``). The Qdrant client is imported
    lazily so the harness can run without the ``[memory]`` extra.

    Payload schema (per point):

    * ``session_id`` (str) — for cross-session isolation
    * ``agent_id`` (str | None) — None for admin context
    * ``level`` (str) — always ``"L2"`` in v1.3.0; reserved for future levels
    * ``created_at`` (float) — epoch seconds
    * ``tags`` (list[str]) — for keyword co-filtering

    The :meth:`search` method passes ``filter`` through to
    Qdrant's native filter DSL — operators can mix payload
    predicates with the dense score without re-implementing
    RRF on the client side.
    """

    def __init__(
        self,
        url: str,
        collection: str = "scratchpad_l2",
        dim: int = 384,
    ) -> None:
        # Lazy import so the harness boots without the [memory] extra.
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm
        self._models = qm
        self._url = url
        self._collection = collection
        self._dim = dim
        # Short timeout: if Qdrant is dead, we want to fall through
        # to SQLite quickly rather than block the chat loop.
        self._client = QdrantClient(url=url, timeout=5.0)
        # Ensure the collection exists. ``recreate_collection`` is
        # safe on first use; an operator wanting to preserve an
        # existing collection can set the name explicitly. We use
        # ``get_collection`` first to avoid the recreate path.
        try:
            self._client.get_collection(collection_name=collection)
        except Exception:  # noqa: BLE001 — collection may not exist
            self._client.create_collection(
                collection_name=collection,
                vectors_config=qm.VectorParams(
                    size=dim,
                    distance=qm.Distance.COSINE,
                ),
            )

    async def upsert(
        self,
        note_id: int,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        point = self._models.PointStruct(
            id=int(note_id),
            vector=list(vector),
            payload=dict(payload),
        )
        # QdrantClient.upsert is sync; offload to a thread so we
        # don't block the event loop. The harness's existing
        # ``asyncio.to_thread`` import is in scope.
        await asyncio.to_thread(
            self._client.upsert,
            collection_name=self._collection,
            points=[point],
        )
        # Phase 4.13A v1.23.0: OnMemoryWrite hook (hot-path safe_fire).
        _fire_on_memory_write(
            note_id=note_id, vector=vector, payload=payload,
        )

    async def search(
        self,
        query_vector: list[float],
        top_k: int,
        *,
        filter: dict[str, Any] | None = None,
    ) -> list[tuple[int, float, dict[str, Any]]]:
        from qdrant_client.http import models as qm
        qm_filter = None
        if filter:
            # Convert the simple ``{key: value}`` filter we use
            # elsewhere to Qdrant's ``FieldCondition`` shape. The
            # Qdrant backend supports more complex predicates via
            # the native DSL; the harness only needs equality.
            must = [
                qm.FieldCondition(
                    field=key,
                    match=qm.MatchValue(value=value),
                )
                for key, value in filter.items()
            ]
            qm_filter = qm.Filter(must=must)
        top_k = max(1, int(top_k))
        result = await asyncio.to_thread(
            self._client.search,
            collection_name=self._collection,
            query_vector=list(query_vector),
            limit=top_k,
            query_filter=qm_filter,
        )
        return [
            (int(hit.id), float(hit.score), dict(hit.payload or {}))
            for hit in result
        ]

    async def delete(self, note_id: int) -> bool:
        from qdrant_client.http import models as qm
        await asyncio.to_thread(
            self._client.delete,
            collection_name=self._collection,
            points_selector=qm.PointIdsList(
                points=[int(note_id)],
            ),
        )
        # Qdrant's delete doesn't tell us whether a point existed.
        # The harness treats "delete called" as success; the caller
        # has its own notion of the source-of-truth note row.
        return True

    async def count(self) -> int:
        info = await asyncio.to_thread(
            self._client.get_collection,
            collection_name=self._collection,
        )
        # ``vectors_count`` is the standard field; fall back to
        # ``points_count`` for older Qdrant versions.
        if hasattr(info, "vectors_count") and info.vectors_count is not None:
            return int(info.vectors_count)
        return int(getattr(info, "points_count", 0) or 0)


# === Factory ===

def make_l2_store(
    qdrant_url: str | None = None,
    collection: str = "scratchpad_l2",
    dim: int = 384,
    db_path: Path | None = None,
) -> L2VectorStore:
    """Pick the best L2 backend for the current environment.

    Order of preference:
      1. Qdrant if ``qdrant_url`` is set AND the server is reachable.
      2. SQLite fallback otherwise (requires ``db_path``).

    The Qdrant probe is best-effort: any exception is caught and
    logged, and we fall through to SQLite. This is the documented
    "Qdrant optional" behaviour from the v1.3.0 design — operators
    who set the URL but happen to have a dead server still get
    working retrieval, just slower (in-memory matrix).
    """
    if qdrant_url:
        try:
            store: L2VectorStore = QdrantL2Store(
                url=qdrant_url,
                collection=collection,
                dim=dim,
            )
            logger.info("L2 vector store: Qdrant @ %s/%s", qdrant_url, collection)
            return store
        except Exception as exc:  # noqa: BLE001 — Qdrant is optional
            logger.warning(
                "Qdrant unavailable (%s: %s); falling back to SQLite",
                type(exc).__name__, exc,
            )
    if db_path is None:
        raise ValueError(
            "make_l2_store: db_path is required for the SQLite fallback"
        )
    logger.info("L2 vector store: SQLite @ %s", db_path)
    return SqliteL2Store(db_path)


__all__ = [
    "L2VectorStore",
    "QdrantL2Store",
    "SqliteL2Store",
    "make_l2_store",
]
