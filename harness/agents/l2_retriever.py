"""L2 retriever — hybrid dense+BM25 search over scratchpad L2 (Phase 3 v1.3.0).

Phase 3 v1.3.0 introduces the "Select" strategy from the Anthropic
context-engineering playbook. The L2 archive of the scratchpad
stores long-term notes; the L2 retriever makes them **discoverable**
by combining:

  * **Dense cosine** over the embeddings stored in
    :class:`~harness.agents.l2_vector_store.L2VectorStore` (Qdrant
    primary, SQLite fallback).
  * **Sparse BM25** over the in-memory Note corpus — fast for
    keyword-heavy queries and the only signal when an L2 note
    has no embedding yet.
  * **Reciprocal Rank Fusion (RRF)** of the two ranked lists with
    ``rrf_k=60`` (Cormack 2009) — score-free fusion that beats
    either retriever alone.

The retriever is the foundation of the LLM-curator pipeline in
:meth:`L2Retriever.curated_search` (Step 2) and the
``scratchpad_l2_search`` tool (Step 3). The retriever itself
stays LLM-free — it's a pure-information-retrieval component.

**Trust boundary:** this module is built on the ``L2VectorStore``
Protocol from :mod:`harness.agents.l2_vector_store`, not on
Qdrant directly. The runner wires the store via factory DI (see
``AgentRunner.l2_retriever_factory`` in Step 3) so the harness
continues to NOT import this module from ``runner.py``.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .l2_vector_store import L2VectorStore
from .scratchpad import Note

logger = logging.getLogger(__name__)


# === Helpers ===

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokenise(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


# === BM25 retriever (in-memory, Note-based) ===

class _BM25Index:
    """Lightweight BM25 over an in-memory list of Notes.

    Mirrors the structure of
    :class:`harness.memory.retrieval.bm25.BM25Retriever` but
    operates on the scratchpad :class:`Note` dataclass. A new
    index is built per query (the L2 archive is small enough
    that the rebuild cost is dominated by the LLM call anyway)
    — for larger corpora the operator should switch to a
    persistent BM25 store (out of scope for v1.3.0).
    """

    _K1: float = 1.5
    _B: float = 0.75

    def __init__(self, notes: list[Note]) -> None:
        self._notes = notes
        self._docs_tokens = [_tokenise(n.content) for n in notes]
        self._doc_freqs: Counter[str] = Counter()
        for tokens in self._docs_tokens:
            for term in set(tokens):
                self._doc_freqs[term] += 1
        self._N = max(len(notes), 1)
        self._avgdl = (
            sum(len(t) for t in self._docs_tokens) / self._N
            if self._docs_tokens else 0.0
        )

    def retrieve(
        self, query: str, k: int,
    ) -> list[tuple[Note, float]]:
        if k <= 0 or not self._notes:
            return []
        q_tokens = _tokenise(query)
        if not q_tokens:
            return []
        scores: list[tuple[int, float]] = []
        for idx, doc_tokens in enumerate(self._docs_tokens):
            if not doc_tokens:
                continue
            score = self._bm25_score(q_tokens, doc_tokens)
            if score > 0:
                scores.append((idx, score))
        if not scores:
            return []
        scores.sort(key=lambda x: (-x[1], x[0]))
        top = scores[:k]
        return [(self._notes[i], s) for i, s in top]

    def _bm25_score(
        self, q_tokens: list[str], doc_tokens: list[str],
    ) -> float:
        dl = len(doc_tokens)
        tf: Counter[str] = Counter(doc_tokens)
        score = 0.0
        for term in q_tokens:
            if term not in tf:
                continue
            df = self._doc_freqs.get(term, 0)
            idf = math.log(((self._N - df + 0.5) / (df + 0.5)) + 1.0)
            tf_norm = (tf[term] * (self._K1 + 1)) / (
                tf[term] + self._K1 * (1 - self._B + self._B * dl / max(self._avgdl, 1e-6))
            )
            score += idf * tf_norm
        return score


# === Dense retriever wrapper ===

@runtime_checkable
class _Embedder(Protocol):
    """Minimal Embedder protocol for the L2 retriever.

    The harness's :class:`harness.memory.embeddings.OnnxEmbedder`
    and :class:`~harness.memory.embeddings.PrivacyAwareEmbedder`
    both conform. We don't import them here (trust boundary):
    the L2 retriever is duck-typed.
    """

    dim: int

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class _CuratorRouter(Protocol):
    """Minimal LLM router protocol for the LLM-curator (Step 2).

    The harness's :class:`harness.server.llm.router.LLMRouter`
    conforms via its ``completion(messages, model, ...) -> CompletionResult``
    method. We only need ``.completion`` here; the protocol
    intentionally omits ``streaming_completion`` to keep the
    surface narrow.
    """

    async def completion(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any: ...


class _DenseRetriever:
    """Dense retriever that delegates to an L2VectorStore.

    The :class:`L2VectorStore` already returns ``(note_id, score,
    payload)`` — we join the ``note_id`` back to a real ``Note``
    object via the in-memory ``id_to_note`` map built from the
    same corpus the BM25 index sees. That keeps the two halves of
    the hybrid in lockstep.
    """

    def __init__(
        self, l2_vec: L2VectorStore, embedder: _Embedder,
        notes: list[Note], *, filter_payload: dict[str, Any] | None = None,
    ) -> None:
        self._l2_vec = l2_vec
        self._embedder = embedder
        self._id_to_note = {int(n.id): n for n in notes}
        self._filter = filter_payload

    async def retrieve(
        self, query: str, k: int,
    ) -> list[tuple[Note, float]]:
        if k <= 0 or not self._id_to_note:
            return []
        q_vec = await self._embedder.embed_query(query)
        hits = await self._l2_vec.search(q_vec, top_k=k, filter=self._filter)
        results: list[tuple[Note, float]] = []
        for note_id, score, _payload in hits:
            note = self._id_to_note.get(int(note_id))
            if note is not None:
                results.append((note, float(score)))
        return results


# === L2Retriever (public API) ===

class L2Retriever:
    """Hybrid dense+BM25 retriever over the scratchpad L2 archive.

    Combines two complementary signals:
      * dense cosine (via :class:`L2VectorStore` + an ``Embedder``)
      * sparse BM25 (in-memory, rebuilt per call)
    via Reciprocal Rank Fusion (``rrf_k=60`` default).

    Args:
        l2_vec:    Dense vector store (Qdrant or SQLite).
        embedder:  Embedder for query-time vectorisation. Duck-typed
                   via the ``_Embedder`` Protocol — any object with
                   ``dim: int`` and ``async embed_query(text)`` works.
        fetch_k:   Per-retriever top-k before fusion. Default 20.
                   The final ``k`` items come from the fused list.
        rrf_k:     RRF smoothing constant. Default 60 (Cormack 2009).
        session_id: Optional session filter — when set, the dense
                   retriever only returns vectors whose payload
                   matches. BM25 still searches the full in-memory
                   corpus (the operator can pre-filter if needed).
    """

    def __init__(
        self,
        l2_vec: L2VectorStore,
        embedder: _Embedder,
        *,
        fetch_k: int = 20,
        rrf_k: int = 60,
        session_id: str | None = None,
    ) -> None:
        self._l2_vec = l2_vec
        self._embedder = embedder
        self._fetch_k = max(1, int(fetch_k))
        self._rrf_k = max(0, int(rrf_k))
        self._session_id = session_id

    async def search(
        self,
        query: str,
        top_k: int = 10,
        *,
        notes: list[Note] | None = None,
    ) -> list[tuple[Note, float]]:
        """Hybrid RRF search over the supplied L2 ``notes`` corpus.

        The BM25 index is built from the in-memory ``notes`` list;
        the dense index queries ``l2_vec`` with the optional
        session_id filter. The two ranked lists are fused via RRF.

        Args:
            query: Free-text query.
            top_k: Number of items to return from the fused list.
            notes: In-memory L2 notes corpus (typically the result
                of ``store.read_notes("L2", limit=...)``). When
                ``None``, an empty corpus is assumed and the
                retriever returns an empty list (the dense path
                still works against ``l2_vec`` but cannot map
                note_ids back to Note objects).

        Returns:
            A list of ``(Note, rrf_score)`` tuples, sorted by RRF
            score descending. RRF scores are in ``[0, ~0.05]`` for
            a 2-retriever fusion — they are NOT comparable across
            queries, only within the same query.
        """
        if top_k <= 0:
            return []
        notes = notes or []
        bm25 = _BM25Index(notes)
        filter_payload: dict[str, Any] | None = None
        if self._session_id is not None:
            filter_payload = {"session_id": self._session_id}
        dense = _DenseRetriever(
            self._l2_vec, self._embedder, notes,
            filter_payload=filter_payload,
        )
        bm25_hits = bm25.retrieve(query, k=self._fetch_k)
        dense_hits = await dense.retrieve(query, k=self._fetch_k)
        return _rrf_fuse(
            bm25_hits, dense_hits, top_k=top_k, rrf_k=self._rrf_k,
        )

    async def curated_search(
        self,
        query: str,
        top_k: int = 10,
        candidate_k: int = 50,
        *,
        notes: list[Note] | None = None,
        router: _CuratorRouter | None = None,
        model: str = "qwen3:8b",
    ) -> list[tuple[Note, float]]:
        """LLM-curator re-rank on top of the hybrid search.

        The flow is:
          1. Pull ``candidate_k`` notes via the plain hybrid
             :meth:`search` (BM25 + dense + RRF).
          2. Build a single-shot curator prompt that asks the
             model to score each note 0-100 by relevance.
          3. Parse the JSON response and re-rank.
          4. On any failure (no router, malformed JSON, model
             refuses) we fall back to the plain hybrid top-K —
             the LLM-curator is a precision enhancement, not a
             correctness gate.

        Args:
            query:       Free-text query.
            top_k:       Final list size.
            candidate_k: How many candidates to hand the LLM. The
                         LLM-curator's value proposition is
                         "narrow N candidates to top-K"; passing
                         the whole corpus defeats the purpose.
            notes:       In-memory L2 notes (same as ``search``).
            router:      An LLM router (duck-typed). When ``None``,
                         we skip the curator and return the plain
                         hybrid result.
            model:       Model id for the curator call. Default
                         ``qwen3:8b`` (T1 in the cost cascade).

        Returns:
            A list of ``(Note, score)`` sorted by score descending.
            Scores are in ``[0, 100]`` after the curator pass, or
            RRF scores ``[0, ~0.05]`` when the curator was skipped.
        """
        if top_k <= 0:
            return []
        # Step 1: hybrid search to get candidates.
        candidates = await self.search(
            query, top_k=candidate_k, notes=notes,
        )
        if not candidates:
            return []
        # Step 2: curator (with fail-open to plain hybrid).
        if router is None:
            logger.debug(
                "L2 curated_search: no router, returning plain hybrid top-%d",
                top_k,
            )
            return candidates[:top_k]
        prompt = _build_curator_prompt(query, [n for n, _ in candidates])
        messages = [
            {"role": "system", "content": "You are a precise relevance scorer."},
            {"role": "user", "content": prompt},
        ]
        try:
            response = await router.completion(
                messages=messages, model=model, tools=None,
            )
            content = getattr(response, "content", None) or ""
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "L2 curated_search: curator LLM call failed (%s); "
                "falling back to plain hybrid top-%d",
                exc, top_k,
            )
            return candidates[:top_k]
        # Step 3: parse + re-rank.
        scored = parse_curator_response(content, [n for n, _ in candidates])
        if not scored:
            logger.warning(
                "L2 curated_search: curator returned no usable scores "
                "(content=%r); falling back to plain hybrid top-%d",
                content[:200], top_k,
            )
            return candidates[:top_k]
        # Sort by curator score descending, then take top_k.
        scored.sort(key=lambda ns: -ns[1])
        return scored[:top_k]


def _rrf_fuse(
    bm25_hits: list[tuple[Note, float]],
    dense_hits: list[tuple[Note, float]],
    *,
    top_k: int,
    rrf_k: int,
) -> list[tuple[Note, float]]:
    """Reciprocal Rank Fusion over two ranked lists.

    RRF is a sum of ``1 / (rrf_k + rank)`` terms across the input
    lists, with rank 1-based. Identical notes appearing in both
    lists naturally accumulate a higher fused score. We dedupe
    by note id — the same note in BM25 and dense contributes
    twice, which is the intended behaviour (it shows up in
    *both* signals).
    """
    scores: dict[int, float] = {}
    notes: dict[int, Note] = {}
    for rank, (note, _s) in enumerate(bm25_hits, start=1):
        nid = int(note.id)
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (rrf_k + rank)
        notes[nid] = note
    for rank, (note, _s) in enumerate(dense_hits, start=1):
        nid = int(note.id)
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (rrf_k + rank)
        notes[nid] = note
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(notes[nid], score) for nid, score in ranked[:top_k]]


# === LLM-curator (Step 2) ===

#: Prompt for the LLM-curator re-ranker. The curator sees the
#: query and the top-N candidate notes, and returns a JSON
#: list of ``{id, score}`` pairs in ``[0, 100]``. We keep the
#: scoring rubric in the prompt so a model swap doesn't break
#: the contract — the harness enforces the contract by parsing
#: the JSON, not by trusting the free-form reasoning.
_CURATOR_PROMPT = (
    "You are a relevance scorer for an agent's scratchpad L2 archive.\n"
    "Given a query and a list of notes, score each note's relevance "
    "to the query on a 0-100 scale.\n"
    "\n"
    "Scoring rubric:\n"
    "  - 90-100: directly answers the query\n"
    "  - 50-89:  related, useful context\n"
    "  - 10-49:  tangentially related\n"
    "  - 0-9:    unrelated\n"
    "\n"
    "Be strict — only notes that DIRECTLY address the query should "
    "score above 50. Prefer precision over recall.\n"
    "\n"
    "Query: __QUERY__\n"
    "\n"
    "Notes:\n"
    "__NOTES__\n"
    "\n"
    'Return ONLY a JSON list: [{"id": 42, "score": 85}, '
    '{"id": 43, "score": 30}]\n'
)


def _format_curator_notes(notes: list[Note]) -> str:
    """Render the candidate notes for the curator prompt.

    The format is intentionally minimal (id + tags + content) so a
    small model (T1 = Qwen3 8B) can scan the list quickly. We
    truncate each note's content to 500 chars to keep the prompt
    bounded for ``candidate_k=50`` candidates.
    """
    lines: list[str] = []
    for n in notes:
        tags = f" [{','.join(n.tags)}]" if n.tags else ""
        content = (n.content or "")[:500]
        lines.append(f"- (id={int(n.id)}){tags} {content}")
    return "\n".join(lines) if lines else "(no candidates)"


def _build_curator_prompt(query: str, candidates: list[Note]) -> str:
    # Use ``replace`` rather than ``str.format`` so the curly braces
    # in the example-JSON (``[{"id": 42, ...}]``) don't trip the
    # format-spec parser. The two placeholders below are unique
    # enough that a literal collision is essentially impossible.
    return (
        _CURATOR_PROMPT
        .replace("__QUERY__", query)
        .replace("__NOTES__", _format_curator_notes(candidates))
    )


def parse_curator_response(
    response_text: str, candidates: list[Note],
) -> list[tuple[Note, float]]:
    """Parse the LLM-curator's JSON response into ``(Note, score)`` pairs.

    Defensive: the LLM may return markdown-fenced JSON (triple-backtick
    ``json ... `` blocks), a trailing newline, a leading "Sure!" preamble,
    or plain malformed output. We strip the common wrappers, attempt
    ``json.loads``, and fall back to an empty list on any error so
    the caller can degrade to the plain hybrid search.
    """
    if not response_text:
        return []
    # Strip common markdown fences and leading prose.
    text = response_text.strip()
    # Find the first '[' and last ']' — keeps JSON even when the
    # model adds pre/postamble.
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end <= start:
        return []
    candidate_str = text[start:end + 1]
    try:
        parsed = json.loads(candidate_str)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    # Build an id → Note map for fast lookup.
    by_id = {int(n.id): n for n in candidates}
    scored: list[tuple[Note, float]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        # Both ``id`` and ``score`` are required — missing either
        # means the LLM didn't follow the schema for this item,
        # so we skip it rather than guessing a default.
        if "id" not in item or "score" not in item:
            continue
        try:
            nid = int(item["id"])
            score = float(item["score"])
        except (TypeError, ValueError):
            continue
        if nid in by_id and 0.0 <= score <= 100.0:
            scored.append((by_id[nid], score))
    return scored


__all__ = [
    "L2Retriever",
    "_build_curator_prompt",
    "parse_curator_response",
]
