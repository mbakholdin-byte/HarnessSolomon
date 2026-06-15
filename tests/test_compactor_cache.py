"""Tests for :mod:`harness.context.compaction` cache integration (Phase 3.5, Step 1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.agents.compact_store import CompactRecord, CompactStore
from harness.config import Settings
from harness.context.compaction import ContextCompactor


# === Fake router (simple class, async def, matches _Summariser Protocol) ===

class _FakeRouter:
    """Minimal async router — counts calls and returns configurable summaries."""

    def __init__(self, summary: str = "summary text") -> None:
        self.summary = summary
        self.call_count = 0

    async def completion(
        self,
        messages: list[dict],
        model: str,
        **kwargs: Any,
    ) -> Any:
        self.call_count += 1
        return _Resp(self.summary)


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


# === Fixtures ===

@pytest.fixture
def settings_with_cache(tmp_path: Path) -> Settings:
    """Settings with persistence enabled; minimal defaults for compactor.

    We use ``keep_recent_turns=50`` (high floor) so the sliding
    window cannot drop enough messages to hit the target on its
    own — this forces the slow path (LLM summary) to run, which
    is what we want to verify for the cache miss / persist path.
    """
    return Settings(
        compaction_enabled=True,
        compaction_threshold_ratio=0.5,
        compaction_target_ratio=0.25,
        compaction_keep_recent_turns=50,
        compaction_summarizer_model="qwen3:8b",
        compaction_summarizer_fallback="glm-4.7",
        compaction_persist_to_memory=False,
        # Phase 3.5 new fields:
        compaction_persistent_store=True,
        compaction_cache_max_versions=5,
        compaction_audit_log=False,
        # Other Settings required fields with sane defaults
        db_path=tmp_path / "harness.db",
    )


@pytest.fixture
def settings_no_cache(tmp_path: Path) -> Settings:
    """Settings with persistence disabled (same tight target)."""
    s = Settings(
        compaction_enabled=True,
        compaction_threshold_ratio=0.5,
        compaction_target_ratio=0.25,
        compaction_keep_recent_turns=50,
        compaction_summarizer_model="qwen3:8b",
        compaction_summarizer_fallback="glm-4.7",
        compaction_persist_to_memory=False,
        compaction_persistent_store=False,
        compaction_cache_max_versions=5,
        compaction_audit_log=False,
        db_path=tmp_path / "harness.db",
    )
    return s


@pytest.fixture
def long_history() -> list[dict[str, Any]]:
    """Return a chat history well over the compactor threshold AND
    large enough that the sliding window cannot meet the target.

    Threshold: 50% of qwen3:8b's 32K ctx = 16384 tokens.
    Target: 25% of 32K = 8192 tokens.
    History: 50 turns of ~700 chars each → ~18K tokens (over threshold).
    With ``keep_recent_turns=50``, the protected tail covers all
    turns; sliding window can't drop anything, so it returns the
    full history at ~9K tokens → still over target → summary runs.
    """
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]
    for i in range(50):
        msgs.append({"role": "user", "content": f"User turn {i}: " + "x" * 680})
        msgs.append({
            "role": "assistant",
            "content": f"Assistant turn {i}: " + "y" * 680,
        })
    return msgs


# === Cache hit path ===

class TestCacheHit:
    async def test_cache_hit_skips_summariser(
        self, tmp_path: Path, settings_with_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """On a cache hit, the LLM router should NOT be called."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        router = _FakeRouter(summary="primary summary")
        compactor = ContextCompactor(
            settings=settings_with_cache,
            router=router,
            memory=None,
            session_id="sess-cache-1",
            store=store,
        )
        # Pre-populate the cache with a fake record matching the
        # expected source_hash.
        from harness.context.compaction import ContextCompactor as CC
        source_hash = CC._source_hash(long_history)
        await store.insert(CompactRecord(
            session_id="sess-cache-1",
            version=0,
            source_hash=source_hash,
            original_tokens=10_000,
            compacted_tokens=200,
            original_message_count=len(long_history),
            kept_message_ids=[],
            summary="cached summary",
            model="qwen3:8b",
            trigger_kind="auto_load_history",
            outcome="ok",
            created_at=0.0,
            duration_ms=50.0,
        ))
        # Call the compactor — should hit the cache and NOT call the router.
        result = await compactor.maybe_compact(
            long_history, "qwen3:8b", session_id="sess-cache-1",
        )
        assert router.call_count == 0  # cache hit = no LLM call
        # The cached summary should appear in the result.
        assert any(
            "cached summary" in str(m.get("content", ""))
            for m in result
        )

    async def test_cache_miss_triggers_summariser_and_persists(
        self, tmp_path: Path, settings_with_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """On a cache miss, the LLM router IS called and the result is persisted."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        router = _FakeRouter(summary="fresh summary")
        compactor = ContextCompactor(
            settings=settings_with_cache,
            router=router,
            memory=None,
            session_id="sess-miss",
            store=store,
        )
        result = await compactor.maybe_compact(
            long_history, "qwen3:8b", session_id="sess-miss",
        )
        assert router.call_count == 1  # slow path ran
        # A new compact record was inserted.
        assert await store.count() == 1
        # The persisted summary text matches what the router returned.
        recs = await store.list_for_session("sess-miss")
        assert recs[0].summary == "fresh summary"
        assert recs[0].version == 1


# === Source hash determinism ===

class TestSourceHash:
    def test_source_hash_deterministic(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        h1 = ContextCompactor._source_hash(msgs)
        h2 = ContextCompactor._source_hash(msgs)
        assert h1 == h2
        assert len(h1) == 16  # truncated to 16 hex chars

    def test_source_hash_changes_with_new_message(self) -> None:
        msgs_a = [{"role": "user", "content": "hello"}]
        msgs_b = [{"role": "user", "content": "hello"}, {"role": "user", "content": "world"}]
        assert ContextCompactor._source_hash(msgs_a) != ContextCompactor._source_hash(msgs_b)

    def test_source_hash_changes_with_reorder(self) -> None:
        msgs_a = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        msgs_b = [
            {"role": "user", "content": "second"},
            {"role": "user", "content": "first"},
        ]
        # Reorder changes the hash (ordering is part of the cache key).
        assert ContextCompactor._source_hash(msgs_a) != ContextCompactor._source_hash(msgs_b)


# === Persistent store disabled ===

class TestPersistentStoreDisabled:
    async def test_setting_disabled_skips_cache(
        self, tmp_path: Path, settings_no_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """When ``compaction_persistent_store=False``, no cache lookup
        or persist happens — even if a store is injected."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        router = _FakeRouter(summary="x")
        compactor = ContextCompactor(
            settings=settings_no_cache,
            router=router,
            memory=None,
            session_id="sess-nocache",
            store=store,
        )
        await compactor.maybe_compact(
            long_history, "qwen3:8b", session_id="sess-nocache",
        )
        # Router WAS called (slow path), but store was NOT written.
        assert router.call_count == 1
        assert await store.count() == 0

    async def test_store_none_skips_cache(
        self, tmp_path: Path, settings_with_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """When ``store=None`` is passed, no cache logic runs."""
        router = _FakeRouter(summary="x")
        compactor = ContextCompactor(
            settings=settings_with_cache,
            router=router,
            memory=None,
            session_id="sess-nostore",
            store=None,
        )
        await compactor.maybe_compact(
            long_history, "qwen3:8b", session_id="sess-nostore",
        )
        # Slow path still works; no errors raised.
        assert router.call_count == 1


# === Cache lookup errors are best-effort ===

class TestCacheErrors:
    async def test_cache_lookup_error_falls_through_to_slow_path(
        self, tmp_path: Path, settings_with_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """A failing cache lookup must not break the compactor."""
        router = _FakeRouter(summary="recovered")
        # Use a store that raises on lookup but is otherwise inert.
        class _BrokenStore(CompactStore):
            async def lookup_cached(self, *args: Any, **kwargs: Any) -> CompactRecord | None:
                raise RuntimeError("simulated SQLite error")

        store = _BrokenStore(tmp_path / "agent-jobs.db")
        await store.init()
        compactor = ContextCompactor(
            settings=settings_with_cache,
            router=router,
            memory=None,
            session_id="sess-broken",
            store=store,
        )
        # Should NOT raise — the compactor swallows the lookup error
        # and falls through to the slow path.
        result = await compactor.maybe_compact(
            long_history, "qwen3:8b", session_id="sess-broken",
        )
        assert router.call_count == 1
        # The recovered summary should be in the output.
        assert any("recovered" in str(m.get("content", "")) for m in result)

    async def test_persist_error_does_not_break_compactor(
        self, tmp_path: Path, settings_with_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """A failing persist (insert) must not break the compactor."""

        class _PersistFailStore(CompactStore):
            async def insert(self, record: CompactRecord) -> int:
                raise RuntimeError("disk full")

        store = _PersistFailStore(tmp_path / "agent-jobs.db")
        await store.init()
        router = _FakeRouter(summary="did-not-persist")
        compactor = ContextCompactor(
            settings=settings_with_cache,
            router=router,
            memory=None,
            session_id="sess-persist-fail",
            store=store,
        )
        # Should NOT raise.
        result = await compactor.maybe_compact(
            long_history, "qwen3:8b", session_id="sess-persist-fail",
        )
        assert router.call_count == 1
        assert any("did-not-persist" in str(m.get("content", "")) for m in result)


# === session_id kwarg ===

class TestSessionIdKwargs:
    async def test_session_id_kwarg_overrides_constructor(
        self, tmp_path: Path, settings_with_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """A per-call ``session_id`` takes precedence over the one in
        ``__init__`` (useful when the session id becomes available
        only at call time)."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        router = _FakeRouter(summary="s")
        compactor = ContextCompactor(
            settings=settings_with_cache,
            router=router,
            memory=None,
            session_id="constructor-id",
            store=store,
        )
        await compactor.maybe_compact(
            long_history, "qwen3:8b", session_id="kwarg-id",
        )
        # The record should be stored under the kwarg's id, not the
        # constructor's.
        recs = await store.list_for_session("kwarg-id")
        assert len(recs) == 1
        assert recs[0].session_id == "kwarg-id"
        assert await store.list_for_session("constructor-id") == []

    async def test_no_session_id_skips_cache(
        self, tmp_path: Path, settings_with_cache: Settings,
        long_history: list[dict[str, Any]],
    ) -> None:
        """When no session_id is provided (constructor or kwarg),
        the cache is bypassed — pre-Phase-3.5 behavior."""
        store = CompactStore(tmp_path / "agent-jobs.db")
        await store.init()
        router = _FakeRouter(summary="s")
        compactor = ContextCompactor(
            settings=settings_with_cache,
            router=router,
            memory=None,
            session_id=None,  # unknown
            store=store,
        )
        # No per-call override either.
        await compactor.maybe_compact(long_history, "qwen3:8b")
        # Slow path ran, but cache is bypassed (no session to key on).
        assert router.call_count == 1
        assert await store.count() == 0


# === Reconstruction from cache ===

class TestRebuildFromCache:
    def test_rebuild_injects_summary_after_system(self, tmp_path: Path) -> None:
        settings = Settings(
            compaction_enabled=True,
            compaction_threshold_ratio=0.5,
            compaction_target_ratio=0.25,
            compaction_keep_recent_turns=2,
            compaction_summarizer_model="qwen3:8b",
            compaction_summarizer_fallback="glm-4.7",
            compaction_persist_to_memory=False,
            compaction_persistent_store=True,
            compaction_cache_max_versions=5,
            compaction_audit_log=False,
            db_path=tmp_path / "harness.db",
        )
        compactor = ContextCompactor(settings=settings, router=_FakeRouter())
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
        ]
        rebuilt = compactor._rebuild_from_cache(msgs, "CACHED")
        # The summary is injected as a user message right after system.
        assert "CACHED" in rebuilt[1]["content"]
        assert rebuilt[0]["role"] == "system"
        assert rebuilt[1]["role"] == "user"
        # The original tail messages are preserved.
        assert any("u1" in str(m.get("content", "")) for m in rebuilt)
