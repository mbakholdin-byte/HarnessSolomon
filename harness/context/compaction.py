"""Phase 3: context compaction (sliding window + LLM summary).

The ``ContextCompactor`` collapses long chat histories before each
LLM call. Two-phase algorithm:

  1. **Sliding window.** Estimate token count of the full ``messages``
     list. If under the threshold (``threshold_ratio * model_ctx``),
     return the input unchanged. Otherwise drop the oldest non-system
     messages, preserving tool-call ↔ tool-result pairs, until the
     list is under ``target_ratio * model_ctx`` or only the protected
     tail (``keep_recent_turns``) + system message remain.

  2. **Summarisation.** If the sliding window still doesn't bring the
     list below the threshold, call the configured summariser model
     (default T1 = local Qwen3 8B, free) with the dropped messages.
     The summary is inserted as a single ``user`` message after the
     system message, with a clear ``[Compaction summary]`` prefix.

The summary is optionally persisted to ``UnifiedMemory`` (L2 mem0)
with tag ``#compact`` so it can be retrieved across sessions via
semantic search. Persistence is best-effort; the compactor does not
block the chat loop on a slow memory write.

Phase 3.5 (v1.1.0) adds an **optional persistent cache** via
:class:`~harness.agents.compact_store.CompactStore`. The cache is
keyed on ``(session_id, source_hash)`` where ``source_hash`` is a
sha256 of the message list before compaction. On a cache hit the
compactor skips the LLM call entirely (zero summariser cost on
reconnect) and returns the cached summary. The compactor remains
stateless w.r.t. the cache — the store is injected via DI, and
``store=None`` preserves the pre-Phase-3.5 in-memory behavior.

**Trust boundary:** the compactor does NOT import the LLM router,
classifier, merge queue, or verifier. ``runner.py`` is constructed
without a compactor (default ``None``), so the trust-boundary test in
``test_agent_runner.py:516-575`` continues to hold. Phase 3.5 keeps
the same trust boundary: ``CompactStore`` is only imported at
lifespan (``app.py``) and injected into the compactor via the
constructor; ``runner.py`` continues to NOT import it.

**Contract:** ``maybe_compact(messages)`` returns a NEW list — it
never mutates the caller's list. This matches the Phase 0
``AgentLoop`` invariant (caller passes the list in, loop mutates
in place; the compactor returns a replacement and the caller rebinds).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from harness.config import Settings
from harness.context.prompts import SUMMARY_SYSTEM_PROMPT
from harness.server.llm.router import LLMRouter

if TYPE_CHECKING:
    from harness.agents.compact_store import CompactRecord, CompactStore
    from harness.context.compaction_audit import CompactionAudit
    from harness.memory.unified import UnifiedMemory

logger = logging.getLogger(__name__)


# Rough heuristic: 1 token ≈ 4 chars in English / JSON serialised
# content. ±15% accuracy; good enough for a sliding window. We avoid
# pulling in tiktoken to keep the dep tree minimal.
_CHARS_PER_TOKEN = 4


class _Summariser(Protocol):
    """Minimal interface needed by the compactor to call a model.

    ``LLMRouter.completion`` already matches this Protocol — the
    Protocol is declared here only to make the dependency explicit
    and to allow tests to inject a fake summariser.
    """

    async def completion(
        self,
        messages: list[dict],
        model: str,
        **kwargs: Any,
    ) -> Any: ...


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate token count of a message list using char/4 heuristic.

    Includes the role + content + tool_calls + tool_call_id overhead.
    For empty / non-list input returns 0.
    """
    if not isinstance(messages, list) or not messages:
        return 0
    total_chars = 0
    for m in messages:
        try:
            total_chars += len(json.dumps(m, ensure_ascii=False))
        except (TypeError, ValueError):
            # Non-serialisable value — fall back to str().
            total_chars += len(str(m))
    return total_chars // _CHARS_PER_TOKEN


def _tool_call_id_set(messages: list[dict[str, Any]]) -> set[str]:
    """Return all tool_call_ids referenced by assistant messages."""
    ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            tcs = m.get("tool_calls") or []
            for tc in tcs:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    ids.add(tc_id)
    return ids


def _keep_for_pairs(idx: int, messages: list[dict[str, Any]]) -> bool:
    """True if the message at ``idx`` is needed to preserve a tool-call
    pair that the kept tail references.

    A tool message is "needed" if any later assistant turn (in the
    kept region) carries a tool_call whose id matches this tool
    message's tool_call_id. Without this, dropping a tool message
    while keeping the assistant turn that requested it breaks the
    OpenAI / Anthropic tool-use contract.
    """
    msg = messages[idx]
    if msg.get("role") != "tool":
        return False
    tool_id = msg.get("tool_call_id")
    if not tool_id:
        return False
    # Scan forward for any assistant turn referencing this id.
    for j in range(idx + 1, len(messages)):
        m = messages[j]
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id") == tool_id:
                    return True
    return False


def _model_ctx(model: str, settings: Settings) -> int:
    """Resolve the context window for ``model`` from the catalog.

    Falls back to a conservative 8192 tokens for unknown models so
    the compactor still works for ad-hoc / prefixed model ids.
    """
    from harness.server.llm.models import get_model

    spec = get_model(model)
    if spec is not None and spec.ctx > 0:
        return spec.ctx
    return 8192


@dataclass
class CompactResult:
    """Phase 3 v1.4.0: result of a manual ``/compact`` invocation.

    Returned by :meth:`ContextCompactor.force_compact` to give the caller
    (HTTP route, CLI subcommand, WebSocket handler) structured feedback
    about what happened — token savings, whether the cache was hit,
    and a short preview of the generated summary.
    """

    original_tokens: int
    compacted_tokens: int
    summary_preview: str
    cache_hit: bool

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.compacted_tokens)


class ContextCompactor:
    """Sliding window + LLM summary.

    The compactor is stateless across calls — it carries config from
    ``__init__`` and does its work in ``maybe_compact``. Safe to share
    across concurrent requests (only the LLM ``completion`` is async
    and uses the router's own concurrency limits).
    """

    def __init__(
        self,
        settings: Settings,
        router: _Summariser,
        memory: "UnifiedMemory | None" = None,
        *,
        session_id: str | None = None,
        store: "CompactStore | None" = None,
        audit: "CompactionAudit | None" = None,
    ) -> None:
        self._settings = settings
        self._router = router
        self._memory = memory
        self._session_id = session_id or "unknown"
        # Phase 3.5: optional persistent cache. ``store=None`` preserves
        # the pre-Phase-3.5 in-memory behavior (no cache, re-summarise
        # on every call). When provided AND ``compaction_persistent_store``
        # is True, the compactor will:
        #   1. compute ``source_hash`` from the input messages
        #   2. look up a cached record by ``(session_id, source_hash)``
        #   3. on hit, skip the LLM summariser entirely
        #   4. on miss, run the full flow + persist the result
        self._store = store
        # Phase 3.5: optional audit log writer. When provided AND
        # ``compaction_audit_log`` is True, every cache hit / miss /
        # persist event is recorded to ``data/audit/compaction-*.ndjson``.
        # When ``audit=None``, the setting is read on each call via
        # ``getattr`` (default False) so the compactor can be
        # constructed without an audit writer and still respect
        # runtime config changes.
        self._audit = audit
        # Resolve summariser model ids: empty string → fallback to
        # the cascade defaults (T1 = Qwen3 8B, T2 = cloud mid-tier).
        self._summariser = (
            settings.compaction_summarizer_model
            or settings.subagent_t1_model
        )
        self._fallback = (
            settings.compaction_summarizer_fallback
            or settings.subagent_t2_model
        )
        # Half of T1's 32K context by default.
        self._max_input_tokens = (
            settings.compaction_summarizer_max_input_tokens
            if settings.compaction_summarizer_max_input_tokens > 0
            else 16000
        )

    async def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compact ``messages`` if over threshold. Returns a NEW list.

        If the input is under threshold OR compaction is disabled, the
        same list object is returned (not a copy — the caller can
        detect no-op via ``result is messages``).

        Parameters
        ----------
        messages:
            Full chat history in OpenAI dict shape.
        model:
            The model id of the calling ``AgentLoop`` — used to look
            up the context window for the threshold check.
        session_id:
            Phase 3.5: optional override of the session id for the
            compact cache lookup. Falls back to the ``session_id``
            passed at construction time. When neither is set, the
            cache is bypassed (pre-Phase-3.5 behavior).

        Algorithm (Phase 3.5):
          1. Token estimate + threshold check.
          2. **Cache lookup** (if ``store`` was injected AND
             ``compaction_persistent_store`` is True): if a record
             with matching ``source_hash`` exists, reconstruct the
             compact and return immediately (zero LLM cost).
          3. Sliding window: drop oldest non-system messages while
             preserving tool pairs and the recent tail.
          4. If still over threshold, summarise the dropped turns via
             the configured model and insert a single summary message.
          5. (Optional) persist the summary to ``UnifiedMemory`` (L2)
             and to ``CompactStore`` (SQLite) — both best-effort.
        """
        if not self._settings.compaction_enabled or not messages:
            return messages
        # Phase 3.5: resolve the effective session id for the cache.
        # The caller may pass a per-call override (e.g. when
        # ``ChatSession.session_id`` becomes available after
        # construction); we fall back to the constructor value.
        effective_session_id = session_id or self._session_id
        ctx = _model_ctx(model, self._settings)
        threshold = int(ctx * self._settings.compaction_threshold_ratio)
        target = int(ctx * self._settings.compaction_target_ratio)
        tokens = _estimate_tokens(messages)
        if tokens <= threshold:
            return messages
        # Phase 3.5: cache lookup (opt-in via setting + store injection).
        # If the same source_hash has been compacted before, we can
        # reconstruct from the cache and skip the (slow + expensive)
        # LLM call entirely. This is the main Phase 3.5 cost-saver.
        # The setting is read via ``getattr`` so Phase 3 tests
        # (which use the old ``Settings`` fixture) don't break before
        # Step 2 adds the new field.
        cache_enabled = getattr(
            self._settings, "compaction_persistent_store", True,
        )
        cache_lookup_start = time.monotonic()
        if (
            cache_enabled
            and self._store is not None
            and effective_session_id != "unknown"
        ):
            try:
                source_hash = self._source_hash(messages)
                cached = await self._store.lookup_cached(
                    effective_session_id, source_hash,
                )
                if cached is not None:
                    # Cache hit: rebuild the compact from the cached
                    # summary + the messages we just received. We
                    # don't need to re-summarise.
                    rebuilt = self._rebuild_from_cache(
                        messages, cached.summary,
                    )
                    cache_ms = (time.monotonic() - cache_lookup_start) * 1000
                    saved_tokens = max(0, tokens - cached.compacted_tokens)
                    logger.info(
                        "compactor.cache_hit session_id=%s version=%d "
                        "saved_tokens=%d saved_ms=%.1f",
                        effective_session_id,
                        cached.version,
                        saved_tokens,
                        cache_ms,
                    )
                    # Phase 3.5: audit log.
                    if self._audit is not None:
                        self._audit.record(
                            "cache_hit",
                            session_id=effective_session_id,
                            version=cached.version,
                            saved_tokens=saved_tokens,
                            duration_ms=cache_ms,
                        )
                    return rebuilt
            except Exception as e:  # noqa: BLE001 — cache is best-effort
                logger.warning(
                    "compactor: cache lookup failed for session_id=%s: %s",
                    effective_session_id, e,
                )
                # Fall through to the slow path.
        # Slow path: sliding window → summarise → persist.
        return await self._run_slow_path(
            messages, model,
            effective_session_id=effective_session_id,
            target=target,
            tokens=tokens,
            cache_enabled=cache_enabled,
        )

    async def _run_slow_path(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        effective_session_id: str,
        target: int,
        tokens: int,
        cache_enabled: bool,
    ) -> list[dict[str, Any]]:
        """Shared slow-path body for ``maybe_compact`` and ``force_compact``.

        Phase 3 v1.4.0: extracted from ``maybe_compact`` so that
        ``force_compact`` (manual ``/compact``) can skip the threshold
        check and always run the trim → summarise → persist pipeline.
        Pure refactor: identical behavior to the previous inline block.
        """
        run_start = time.monotonic()
        # Step 1: sliding window.
        trimmed = self._sliding_window(messages, target)
        if _estimate_tokens(trimmed) <= target:
            return trimmed
        # Step 2: drop the oldest non-system, non-recent block, summarise
        # the dropped part, and prepend the summary after the system
        # message. The summariser may be slow; we still cap total cost
        # by passing only the dropped region (already <= max_input_tokens
        # via the sliding window pre-trim).
        dropped_region = self._extract_dropped(messages, trimmed)
        if not dropped_region:
            return trimmed
        summary = await self._summarise(dropped_region)
        if not summary:
            return trimmed  # summariser failed — fall back to raw trim
        compacted = self._inject_summary(trimmed, summary)
        # Step 3: persist to UnifiedMemory (best-effort, fire-and-forget).
        if self._settings.compaction_persist_to_memory and self._memory is not None:
            try:
                await self._persist_summary(summary)
            except Exception as e:  # noqa: BLE001 — audit is best-effort
                logger.warning("compaction: persist to memory failed: %s", e)
        # Step 4 (Phase 3.5): persist to CompactStore for cache hits
        # on future calls. We do this last so a slow store write
        # doesn't delay the chat loop.
        if (
            cache_enabled
            and self._store is not None
            and effective_session_id != "unknown"
        ):
            try:
                version = await self._persist_compact(
                    session_id=effective_session_id,
                    source_hash=self._source_hash(messages),
                    original_tokens=tokens,
                    compacted_tokens=_estimate_tokens(compacted),
                    original_message_count=len(messages),
                    kept_message_ids=[],  # not tracked in Phase 3.5 Step 1
                    summary=summary,
                    model=self._summariser or "unknown",
                    trigger_kind="auto_load_history",
                    outcome="ok",
                    duration_ms=(time.monotonic() - run_start) * 1000,
                )
                # Phase 3.5: audit log for successful run.
                if self._audit is not None and version is not None:
                    self._audit.record(
                        "run",
                        session_id=effective_session_id,
                        outcome="ok",
                        version=version,
                        original_tokens=tokens,
                        compacted_tokens=_estimate_tokens(compacted),
                        duration_ms=(time.monotonic() - run_start) * 1000,
                    )
            except Exception as e:  # noqa: BLE001 — cache is best-effort
                logger.warning(
                    "compactor: persist to compact_store failed: %s", e,
                )
                # Phase 3.5: audit log for failed persist.
                if self._audit is not None:
                    self._audit.record(
                        "persist_failed",
                        session_id=effective_session_id,
                        error=str(e),
                    )
        return compacted

    async def force_compact(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        session_id: str | None = None,
        bypass_cache: bool = False,
    ) -> CompactResult:
        """Force a compact regardless of token threshold.

        Phase 3 v1.4.0: public API for the manual ``/compact`` slash
        (CLI subcommand, HTTP route, WebSocket message). Unlike
        :meth:`maybe_compact`, this method ALWAYS runs the slow path
        (sliding window → summarise → persist) and returns a
        :class:`CompactResult` with token savings + summary preview.

        The threshold check is bypassed (``force`` semantics). The
        cache may optionally be bypassed too (``bypass_cache=True``)
        to force a re-summarisation even if a cached record exists
        for the same source_hash.

        Parameters
        ----------
        messages:
            Full chat history in OpenAI dict shape.
        model:
            Model id of the calling ``AgentLoop`` — used to look up
            the context window for the sliding window's target.
        session_id:
            Optional override of the session id for the cache lookup.
        bypass_cache:
            When True, the CompactStore cache is skipped on both the
            lookup (no cache hit) and the persist (no record written).
            Defaults to False (cache behaves like ``maybe_compact``).

        Returns
        -------
        CompactResult
            Structured feedback for the caller: original/compacted
            token counts, summary preview, cache_hit flag.
        """
        if not messages:
            return CompactResult(
                original_tokens=0,
                compacted_tokens=0,
                summary_preview="",
                cache_hit=False,
            )
        effective_session_id = session_id or self._session_id
        ctx = _model_ctx(model, self._settings)
        # Force threshold=0 so the slow path always runs. Target stays
        # the same as ``maybe_compact`` (50% of model context).
        target = int(ctx * self._settings.compaction_target_ratio)
        tokens = _estimate_tokens(messages)
        # Phase 3 v1.4.0: cache lookup — when bypass_cache=False and
        # the same source_hash has been compacted before, return the
        # cached result immediately (zero LLM cost).
        cache_enabled = (
            getattr(self._settings, "compaction_persistent_store", True)
            and not bypass_cache
        )
        if (
            cache_enabled
            and self._store is not None
            and effective_session_id != "unknown"
        ):
            try:
                source_hash = self._source_hash(messages)
                cached = await self._store.lookup_cached(
                    effective_session_id, source_hash,
                )
                if cached is not None:
                    rebuilt = self._rebuild_from_cache(
                        messages, cached.summary,
                    )
                    logger.info(
                        "compactor.force_compact.cache_hit "
                        "session_id=%s version=%d",
                        effective_session_id, cached.version,
                    )
                    if self._audit is not None:
                        self._audit.record(
                            "manual_compact",
                            session_id=effective_session_id,
                            cache_hit=True,
                            version=cached.version,
                            original_tokens=tokens,
                            compacted_tokens=cached.compacted_tokens,
                        )
                    preview = (cached.summary[:200] + "…") if len(
                        cached.summary
                    ) > 200 else cached.summary
                    return CompactResult(
                        original_tokens=tokens,
                        compacted_tokens=cached.compacted_tokens,
                        summary_preview=preview,
                        cache_hit=True,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "compactor: force_compact cache lookup failed: %s", e,
                )
                # Fall through to slow path.
        # Slow path (no cache hit, or cache bypassed).
        compacted = await self._run_slow_path(
            messages, model,
            effective_session_id=effective_session_id,
            target=target,
            tokens=tokens,
            cache_enabled=cache_enabled,
        )
        compacted_tokens = _estimate_tokens(compacted)
        # Extract the summary preview from the injected summary message.
        preview = ""
        for m in compacted:
            if m.get("role") == "system" and "[Conversation summary]" in (
                m.get("content") or ""
            ):
                content = m.get("content", "")
                # The summary content lives between the marker and
                # the next \n\n (preserved by ``_inject_summary``).
                start = content.find("[Conversation summary]")
                if start >= 0:
                    tail = content[start:].split("\n\n", 1)
                    preview = tail[0] if tail else content[start:start + 200]
                break
        if not preview:
            preview = "(no summary generated)"
        if len(preview) > 200:
            preview = preview[:200] + "…"
        # Phase 3 v1.4.0: audit log for manual compact.
        if self._audit is not None:
            self._audit.record(
                "manual_compact",
                session_id=effective_session_id,
                cache_hit=False,
                original_tokens=tokens,
                compacted_tokens=compacted_tokens,
            )
        return CompactResult(
            original_tokens=tokens,
            compacted_tokens=compacted_tokens,
            summary_preview=preview,
            cache_hit=False,
        )

    def _sliding_window(
        self,
        messages: list[dict[str, Any]],
        target_tokens: int,
    ) -> list[dict[str, Any]]:
        """Drop oldest non-system messages until under target or only the
        protected tail remains.

        Protected set: ``messages[0]`` (system) + last
        ``keep_recent_turns`` messages + any tool message needed to
        preserve a tool-call pair with a kept assistant turn.
        """
        if not messages:
            return messages
        keep_recent = max(2, self._settings.compaction_keep_recent_turns)
        n = len(messages)
        # The "deletion zone" is everything between the system message
        # and the recent tail. Messages outside this zone are
        # always protected.
        sys_idx = 0 if messages[0].get("role") == "system" else None
        recent_start = max(0, n - keep_recent)
        # Walk every index, decide keep-or-drop.
        result: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            # Always keep the system message.
            if i == sys_idx:
                result.append(m)
                continue
            # Always keep the recent tail.
            if i >= recent_start:
                result.append(m)
                continue
            # Keep tool messages needed to preserve a forward-referenced
            # tool-call pair (the assistant turn is in the recent tail).
            if _keep_for_pairs(i, messages):
                result.append(m)
                continue
            # Otherwise, the message is a candidate for summarisation.
            # We still INCLUDE it here; ``_extract_dropped`` later
            # picks these out as the summariser input. The sliding
            # window is therefore "include candidates in the trim",
            # and the summariser produces a single summary message
            # that REPLACES them (not drops them).
        # If the result is already under the target, return it.
        if _estimate_tokens(result) <= target_tokens:
            return result
        # Otherwise, do a hard drop: remove oldest from the candidate
        # zone (between system and recent tail) until under target.
        # This is a fallback for the rare case where summarisation
        # itself is disabled or fails.
        sys_msg = messages[0] if sys_idx is not None else None
        tail = list(messages[recent_start:])
        # Build the candidate zone in order, then add back from the
        # END until under target.
        candidate_zone: list[dict[str, Any]] = []
        for i in range((sys_idx or 0) + 1, recent_start):
            m = messages[i]
            if _keep_for_pairs(i, messages):
                # This tool message must stay paired with the kept
                # assistant in the tail — keep it adjacent to the
                # tail.
                continue
            candidate_zone.append(m)
        # Build the result by taking the most recent candidates first.
        trimmed: list[dict[str, Any]] = []
        if sys_msg is not None:
            trimmed.append(sys_msg)
        for m in reversed(candidate_zone):
            if _estimate_tokens(trimmed + [m] + tail) > target_tokens:
                break
            trimmed.append(m)
        trimmed.reverse()  # restore chronological order
        trimmed.extend(tail)
        return trimmed

    def _extract_dropped(
        self,
        original: list[dict[str, Any]],
        trimmed: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        r"""Return the messages that were dropped (original \ trimmed).

        Used as input to the summariser. Order preserved.
        """
        # Heuristic: dropped = original[protected_start:protected_end - keep_recent]
        if not original or not trimmed:
            return []
        protected_start = 1 if original[0].get("role") == "system" else 0
        keep_recent = max(2, self._settings.compaction_keep_recent_turns)
        drop_start = protected_start
        drop_end = max(drop_start, len(original) - keep_recent)
        if drop_end <= drop_start:
            return []
        return list(original[drop_start:drop_end])

    async def _summarise(
        self, dropped: list[dict[str, Any]],
    ) -> str:
        """Call the summariser model on the dropped messages.

        Falls back to the configured fallback model on error. Returns
        empty string on total failure (caller treats as no-op).
        """
        # Pre-trim the input to the configured max input tokens.
        if _estimate_tokens(dropped) > self._max_input_tokens:
            # Truncate from the front (keep the most recent dropped
            # turns — they're the closest to the kept tail).
            max_chars = self._max_input_tokens * _CHARS_PER_TOKEN
            blob = json.dumps(dropped, ensure_ascii=False)
            if len(blob) > max_chars:
                blob = "…" + blob[-max_chars:]
            dropped = [{"role": "user", "content": f"Earlier turns (truncated):\n{blob}"}]
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(dropped, ensure_ascii=False)},
        ]
        for model in (self._summariser, self._fallback):
            if not model:
                continue
            try:
                result = await self._router.completion(
                    messages=messages,
                    model=model,
                )
                content = getattr(result, "content", "") or ""
                if content.strip():
                    return content.strip()
            except Exception as e:  # noqa: BLE001 — fallback chain
                logger.warning(
                    "compaction: summariser %r failed: %s; trying fallback",
                    model, e,
                )
                continue
        return ""

    def _inject_summary(
        self,
        messages: list[dict[str, Any]],
        summary: str,
    ) -> list[dict[str, Any]]:
        """Insert the summary as a user message right after the system message.

        The injected message is clearly marked so a future compaction
        pass recognises it and does not try to re-summarise.
        """
        marker = "[Compaction summary — earlier turns condensed]"
        body = f"{marker}\n\n{summary}"
        new_msg = {"role": "user", "content": body}
        # Insert after the system message (presumed at index 0).
        if messages and messages[0].get("role") == "system":
            return [messages[0], new_msg, *messages[1:]]
        return [new_msg, *messages]

    async def _persist_summary(self, summary: str) -> None:
        """Write the compaction summary to UnifiedMemory L2 with tag
        ``#compact``. Best-effort; never raises."""
        if self._memory is None:
            return
        from harness.memory.schema import Memory, MemoryLayer

        mem = Memory(
            layer=MemoryLayer.L2,
            source="compact",
            content=summary,
            tags=["#compact", f"#session/{self._session_id}"],
            metadata={"session_id": self._session_id, "kind": "compaction"},
        )
        await self._memory.write(mem)

    # === Phase 3.5: persistent cache ===

    @staticmethod
    def _source_hash(messages: list[dict[str, Any]]) -> str:
        """Return a 16-hex-char fingerprint of ``messages`` for cache lookup.

        Uses ``sha256`` of ``json.dumps(..., sort_keys=True)`` so:
          - message reorder produces a different hash (preserves
            ordering as part of the cache key)
          - insertion of a new message produces a different hash
            (auto-invalidation — no explicit invalidation needed)
          - collision risk is ~2^-64 (16 hex chars)

        The full 64-hex-char digest is overkill for a local cache;
        truncating to 16 chars saves space in SQLite while still
        giving a vanishingly small collision rate.
        """
        try:
            blob = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            # Non-serialisable message — fall back to str() per item.
            blob = "|".join(str(m) for m in messages)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def _rebuild_from_cache(
        self,
        messages: list[dict[str, Any]],
        cached_summary: str,
    ) -> list[dict[str, Any]]:
        """Reconstruct a compact from a cached summary + the current
        message list.

        The cache only stores the summary text (not the full
        post-compact message list) — to rebuild we need to know
        which messages are the "kept tail". Phase 3.5 Step 1 takes
        the simple approach: take the sliding-window result of the
        *current* messages and inject the cached summary in the same
        position. This is slightly different from the original
        compact (different sliding window if the kept tail changed)
        but guarantees the output respects the current
        ``keep_recent_turns`` floor.

        If the input is now smaller than threshold (a new short
        session), the sliding window is a no-op and we return the
        original messages unchanged.
        """
        # Use the same sliding window algorithm as the slow path.
        # ``target`` is supplied at the call site via ``_sliding_window``,
        # but we don't have the call-site context here — so we use
        # the same heuristic: keep the recent tail + system message.
        # This matches the slow path's structure exactly.
        from harness.config import Settings  # noqa: F401 — typing-only
        # Reuse _sliding_window with a generous target so the
        # windowing doesn't drop anything. The summary gets
        # injected at the canonical position (after system).
        target_tokens = 10**9  # effectively "don't drop"
        trimmed = self._sliding_window(messages, target_tokens)
        return self._inject_summary(trimmed, cached_summary)

    async def _persist_compact(
        self,
        *,
        session_id: str,
        source_hash: str,
        original_tokens: int,
        compacted_tokens: int,
        original_message_count: int,
        kept_message_ids: list[int],
        summary: str,
        model: str,
        trigger_kind: str,
        outcome: str,
        duration_ms: float,
    ) -> int | None:
        """Insert a new compact record into the persistent store.

        Returns the assigned ``version`` on success, ``None`` on
        failure (the caller logs and moves on). Best-effort — the
        compactor does not abort the chat loop on a store error.
        """
        from harness.agents.compact_store import CompactRecord

        record = CompactRecord(
            session_id=session_id,
            version=0,  # overwritten by insert()
            source_hash=source_hash,
            original_tokens=original_tokens,
            compacted_tokens=compacted_tokens,
            original_message_count=original_message_count,
            kept_message_ids=kept_message_ids,
            summary=summary,
            model=model,
            trigger_kind=trigger_kind,
            outcome=outcome,
            created_at=time.time(),
            duration_ms=duration_ms,
        )
        try:
            version = await self._store.insert(record)
        except Exception as e:  # noqa: BLE001 — store is best-effort
            logger.warning(
                "compactor: persist_compact failed for session_id=%s: %s",
                session_id, e,
            )
            return None
        logger.info(
            "compactor.run outcome=%s version=%d session_id=%s "
            "original_tokens=%d compacted_tokens=%d duration_ms=%.1f",
            outcome, version, session_id,
            original_tokens, compacted_tokens, duration_ms,
        )
        return version
