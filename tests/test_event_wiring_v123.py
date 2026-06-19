"""Phase 4.13A v1.23.0: wire 3 remaining event hooks.

8 tests covering the 3 custom events that were previously declared in
``harness.hooks.events.EventType`` but had NO trigger-point wiring in
the layers below the original Phase 4.4 sites:

  * **OnMemoryWrite** — fired from ``L2VectorStore.upsert()``
    (both ``SqliteL2Store`` and ``QdrantL2Store``). This complements
    the existing ``UnifiedMemory.write`` site — L2 store upserts are
    a distinct trigger (the schema layer stores a vector + payload
    independent of the unified dual-write path).
  * **OnCompaction** — fired from ``CompactTrigger.compact_now()``
    after a successful ``force_compact``. This complements the
    existing ``ContextCompactor`` emission — ``CompactTrigger`` is
    the manual ``/compact`` entry point and exposes a different
    payload shape (``pre_tokens``, ``post_tokens``, ``ratio``,
    ``trigger_reason``).
  * **OnRoutingDecision** — fired from ``TierSelector.select()``.
    This complements the existing ``LLMRouterClassifier.classify``
    site — ``TierSelector`` is the cost-aware tier cascade (T1/T2/T3)
    and is the authoritative decision point for which model handles
    the call.

All 3 sites use the **hot-path** wrapper ``safe_fire()`` (not
``PermissionRequest``) — hook failures never break the trigger path.

Scope:
    * Each event fires exactly once for a single trigger action.
    * Payload contains the documented Phase 4.13A fields.
    * The L2 store module does NOT import ``harness.agents`` (the
      hook is imported lazily via ``harness.hooks.runner.safe_fire``).
    * A registered silent hook does not block the hot path
      (latency < 5ms overhead from the hook machinery alone).
"""
from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import numpy as np
import pytest

from harness.agents.cascade import CascadeDecision, TierSelector, TIER_T2
from harness.agents.l2_vector_store import SqliteL2Store
from harness.hooks.context import HookContext, HookDecision
from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec, reset_registry
from harness.hooks.runner import (
    HookRunner,
    set_global_hook_runner,
)
from harness.server.agent.compact_trigger import CompactTrigger


# === Helpers ============================================================


def _unit_vector(seed: int, dim: int = 4) -> list[float]:
    """Deterministic L2-normalised vector for tests."""
    rng = np.random.default_rng(seed)
    v = rng.random(dim).astype(np.float32)
    v /= np.linalg.norm(v)
    return v.tolist()


_NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS scratchpad_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    level TEXT NOT NULL CHECK(level IN ('L0','L1','L2')),
    content TEXT NOT NULL,
    tags TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""


async def _init_scratchpad_db(db_path: Path) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_NOTES_SCHEMA)
        await db.commit()


async def _seed_note_row(
    db_path: Path, note_id: int, session_id: str, level: str = "L2",
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO scratchpad_notes "
            "(id, session_id, agent_id, level, content, tags, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (note_id, session_id, "a1", level, "content", "[]", 12345.0),
        )
        await db.commit()


class FakeCompactResult:
    """Mimics :class:`harness.context.compaction.CompactResult`."""

    def __init__(
        self,
        *,
        original_tokens: int = 1000,
        compacted_tokens: int = 200,
        summary_preview: str = "summary",
        cache_hit: bool = False,
    ) -> None:
        self.original_tokens = original_tokens
        self.compacted_tokens = compacted_tokens
        self.summary_preview = summary_preview
        self.cache_hit = cache_hit

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.compacted_tokens)


@pytest.fixture
def fresh_runner() -> Iterator[HookRunner]:
    """Bind a clean global ``HookRunner`` with an empty registry."""
    registry = HookRegistry()
    runner = HookRunner(registry, default_timeout_ms=500)
    set_global_hook_runner(runner)
    yield runner
    set_global_hook_runner(None)
    reset_registry()


@pytest.fixture(autouse=True)
def _reset_global_runner() -> Iterator[None]:
    """Ensure no leftover global runner leaks between tests."""
    set_global_hook_runner(None)
    reset_registry()
    yield
    set_global_hook_runner(None)
    reset_registry()


def _make_recorder(
    event: str,
    *,
    decision: str = "allow",
    seen: list[HookContext] | None = None,
):
    """Build a builtin hook callable that records the dispatched context."""

    async def _hook(ctx: HookContext) -> HookDecision:
        if seen is not None:
            seen.append(ctx)
        return HookDecision(decision=decision, hook_id=f"test.recorder.{event}")

    return _hook


async def _register_hook(
    runner: HookRunner,
    event_type: EventType,
    *,
    seen: list[HookContext] | None = None,
    decision: str = "allow",
) -> None:
    await runner._registry.register(  # noqa: SLF001 — test-only
        HookSpec(
            hook_id=f"test.recorder.{event_type.value}",
            event=event_type,
            transport="builtin",
            callable=_make_recorder(event_type.value, decision=decision, seen=seen),
        )
    )


# === 1-3: OnMemoryWrite (L2 store upsert) ==============================


async def test_on_memory_write_fires_on_l2_upsert(
    tmp_path: Path, fresh_runner: HookRunner,
) -> None:
    """``SqliteL2Store.upsert`` → fires ``OnMemoryWrite`` exactly once."""
    db = tmp_path / "l2.db"
    await _init_scratchpad_db(db)
    await _seed_note_row(db, 1, "s1")
    store = SqliteL2Store(db)

    seen: list[HookContext] = []
    await _register_hook(
        fresh_runner, EventType.ON_MEMORY_WRITE, seen=seen,
    )
    await store.upsert(1, _unit_vector(1), {"session_id": "s1", "agent_id": "a1"})

    # safe_fire is scheduled via loop.create_task; allow it to run.
    await asyncio_sleep_until_hook_seen(seen, timeout=1.0)
    assert len(seen) == 1, (
        f"expected 1 OnMemoryWrite dispatch for L2 upsert, got {len(seen)}"
    )
    assert seen[0].event == "OnMemoryWrite"


async def test_on_memory_write_includes_layer_and_size(
    tmp_path: Path, fresh_runner: HookRunner,
) -> None:
    """``OnMemoryWrite`` payload for L2 carries layer + size fields."""
    db = tmp_path / "l2.db"
    await _init_scratchpad_db(db)
    await _seed_note_row(db, 1, "s1")
    store = SqliteL2Store(db)

    seen: list[HookContext] = []
    await _register_hook(
        fresh_runner, EventType.ON_MEMORY_WRITE, seen=seen,
    )
    vec = _unit_vector(1)
    await store.upsert(1, vec, {"session_id": "s1", "agent_id": "a1"})

    await asyncio_sleep_until_hook_seen(seen, timeout=1.0)
    assert len(seen) == 1
    payload = seen[0].payload
    assert payload["layer"] == "L2"
    # value_size == len(vector) * 4 bytes (float32).
    assert payload["value_size"] == len(vec) * 4
    # Schema-required size_bytes matches value_size.
    assert payload["size_bytes"] == payload["value_size"]
    # PII safety: raw key is NOT placed in the payload, only key_hash.
    assert "key_hash" in payload
    assert len(payload["key_hash"]) == 16
    # PII guard: no raw ``key`` / ``value`` fields (schema-level).
    assert "key" not in payload
    assert "value" not in payload
    # Phase 4.13A spec fields propagated (note_id, not raw key).
    assert payload["note_id"] == 1
    assert payload["session_id"] == "s1"
    assert payload["agent_id"] == "a1"
    assert "timestamp" in payload


def test_on_memory_write_no_harness_agents_import() -> None:
    """``harness.agents.l2_vector_store`` MUST NOT import harness.agents
    at the module level (apart from its own package ``.scratchpad``).

    The OnMemoryWrite wiring uses ``harness.hooks.runner.safe_fire``,
    which is the trust-boundary-safe entry point. Importing
    ``harness.agents`` from within ``l2_vector_store`` would be
    circular (l2_vector_store IS part of harness.agents).
    """
    path = (
        Path(__file__).parent.parent
        / "harness"
        / "agents"
        / "l2_vector_store.py"
    )
    assert path.is_file(), f"expected {path}"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == "harness.agents"
                or node.module.startswith("harness.agents.")
            ):
                # relative import from .scratchpad is OK (level > 0).
                if node.level and node.level > 0:
                    continue
                violations.append(
                    f"line {node.lineno}: from {node.module!r} import ..."
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "harness.agents" or alias.name.startswith(
                    "harness.agents."
                ):
                    violations.append(
                        f"line {node.lineno}: import {alias.name!r}"
                    )
    assert not violations, (
        "l2_vector_store.py must not import harness.agents "
        "(would be circular); violations:\n  " + "\n  ".join(violations)
    )


# === 4-5: OnCompaction (CompactTrigger) ================================


def _make_compactor(result: Any) -> MagicMock:
    compactor = MagicMock()
    compactor.force_compact = AsyncMock(return_value=result)
    return compactor


def _make_settings(*, manual_compact_max_ms: int = 30_000) -> Any:
    return MagicMock(manual_compact_max_ms=manual_compact_max_ms)


async def test_on_compaction_fires_on_compact_trigger(
    fresh_runner: HookRunner,
) -> None:
    """``CompactTrigger.compact_now`` fires ``OnCompaction`` once."""
    result = FakeCompactResult(
        original_tokens=1000, compacted_tokens=200, cache_hit=False,
    )
    trigger = CompactTrigger(_make_compactor(result), _make_settings())
    seen: list[HookContext] = []
    await _register_hook(
        fresh_runner, EventType.ON_COMPACTION, seen=seen,
    )
    out = await trigger.compact_now(
        [{"role": "user", "content": "x"}],
        model="m",
        session_id="s-413",
    )
    assert out is not None, "compact_now must succeed (audit + hook side-effect)"
    assert len(seen) == 1, (
        f"expected 1 OnCompaction dispatch, got {len(seen)}"
    )
    assert seen[0].event == "OnCompaction"


async def test_on_compaction_includes_ratio_and_reason(
    fresh_runner: HookRunner,
) -> None:
    """OnCompaction payload carries pre/post tokens, ratio, reason."""
    result = FakeCompactResult(
        original_tokens=1000, compacted_tokens=250, cache_hit=False,
    )
    trigger = CompactTrigger(_make_compactor(result), _make_settings())
    seen: list[HookContext] = []
    await _register_hook(
        fresh_runner, EventType.ON_COMPACTION, seen=seen,
    )
    await trigger.compact_now(
        [{"role": "user", "content": "x"}],
        model="m",
        session_id="s-ratio",
        bypass_cache=True,
    )
    assert len(seen) == 1
    payload = seen[0].payload
    assert payload["pre_tokens"] == 1000
    assert payload["post_tokens"] == 250
    # ratio = post / pre = 0.25
    assert payload["ratio"] == pytest.approx(0.25, abs=1e-3)
    assert payload["trigger_reason"] == "manual_bypass_cache"
    assert payload["session_id"] == "s-ratio"


# === 6-7: OnRoutingDecision (TierSelector) =============================


def test_on_routing_decision_fires_on_tier_select(
    fresh_runner: HookRunner,
) -> None:
    """``TierSelector.select()`` fires ``OnRoutingDecision`` once.

    ``TierSelector.select_tier`` is the pure-function decision; the new
    ``select()`` wrapper is the Phase 4.13A instrumented entry point.
    safe_fire is async but scheduled via ``loop.create_task`` from
    the sync method, so we need an event loop in scope to observe the
    dispatch.
    """
    import asyncio

    selector = TierSelector(
        t1_model="qwen3:8b",
        t2_model="glm-4.7",
        t3_model="MiniMax-M2.7",
        confidence_high=0.8,
        confidence_low=0.5,
    )
    seen: list[HookContext] = []

    async def _drive() -> CascadeDecision:
        await _register_hook(
            fresh_runner, EventType.ON_ROUTING_DECISION, seen=seen,
        )
        # confidence in [low, high) → T2.
        decision = selector.select(
            0.6,
            prompt_tokens=500,
            session_id="s-tier",
            agent_id="agent-1",
        )
        # Yield control so loop.create_task(safe_fire(...)) can run.
        # safe_fire runs the hook registry synchronously inside the
        # task body (no further awaits in the builtin transport).
        await asyncio.sleep(0.05)
        return decision

    decision = asyncio.run(_drive())
    assert decision.tier == TIER_T2
    assert len(seen) == 1, (
        f"expected 1 OnRoutingDecision dispatch, got {len(seen)}"
    )
    assert seen[0].event == "OnRoutingDecision"
    assert seen[0].payload["selected_tier"] == "T2"
    assert seen[0].payload["model_id"] == "glm-4.7"


def test_on_routing_decision_includes_latency_and_cost(
    fresh_runner: HookRunner,
) -> None:
    """OnRoutingDecision payload includes latency_ms and cost_usd."""
    import asyncio

    selector = TierSelector(
        t1_model="qwen3:8b",
        t2_model="glm-4.7",
        t3_model="MiniMax-M2.7",
        confidence_high=0.8,
        confidence_low=0.5,
    )
    seen: list[HookContext] = []

    async def _drive() -> CascadeDecision:
        await _register_hook(
            fresh_runner, EventType.ON_ROUTING_DECISION, seen=seen,
        )
        # confidence < low → T3.
        decision = selector.select(
            0.1,
            prompt_tokens=1234,
            session_id="s-cost",
            agent_id="agent-2",
        )
        await asyncio.sleep(0.05)
        return decision

    decision = asyncio.run(_drive())
    assert decision.tier == "T3"
    assert len(seen) == 1
    payload = seen[0].payload
    assert payload["prompt_tokens"] == 1234
    assert payload["selected_tier"] == "T3"
    assert "latency_ms" in payload
    assert isinstance(payload["latency_ms"], (int, float))
    assert payload["latency_ms"] >= 0.0
    assert payload["cost_usd"] == 0.0  # TierSelector has no cost table
    assert payload["model_id"] == "MiniMax-M2.7"
    assert payload["session_id"] == "s-cost"
    assert payload["agent_id"] == "agent-2"


# === 8: silent hook does not block hot path ============================


def test_silent_hook_does_not_block_hot_path(
    fresh_runner: HookRunner,
) -> None:
    """A registered silent hook adds <5ms overhead on the hot path.

    The hook is a no-op (just returns ``allow``); the assertion is
    that the TierSelector.select path stays well under the 5ms budget
    even with the hook dispatch machinery (runner, registry lookup,
    asyncio gather) in scope.

    This is the regression guard for Phase 4.13A acceptance: the hook
    wiring MUST be hot-path-safe.
    """
    import asyncio

    selector = TierSelector(
        t1_model="qwen3:8b",
        t2_model="glm-4.7",
        t3_model="MiniMax-M2.7",
        confidence_high=0.8,
        confidence_low=0.5,
    )

    async def _drive(n_calls: int = 50) -> None:
        # Register a silent no-op hook (already-fresh runner).
        seen: list[HookContext] = []
        await _register_hook(
            fresh_runner, EventType.ON_ROUTING_DECISION, seen=seen,
        )
        # Warm-up: first call constructs tasks lazily.
        selector.select(0.6, prompt_tokens=100, session_id="warmup")
        await asyncio.sleep(0.05)
        # Measured: 50 calls, total time / n = per-call overhead.
        start = time.monotonic()
        for _ in range(n_calls):
            selector.select(0.6, prompt_tokens=100, session_id="bench")
        # Yield once at the end so the last batch of create_task
        # callbacks complete (they are not on the measured path —
        # the timer stops here before the await).
        elapsed_s = time.monotonic() - start
        await asyncio.sleep(0.05)
        # Per-call budget: 5 ms (50 calls × 5 ms = 250 ms total ceiling).
        per_call_ms = (elapsed_s / n_calls) * 1000.0
        assert per_call_ms < 5.0, (
            f"silent hook overhead {per_call_ms:.2f}ms/call exceeds 5ms "
            f"budget (total {elapsed_s:.3f}s for {n_calls} calls)"
        )
        # The hook fired at least once during the measured window.
        assert len(seen) >= 1, "silent hook should have fired at least once"

    asyncio.run(_drive())


# === Utilities =========================================================


async def asyncio_sleep_until_hook_seen(
    seen: list[HookContext], *, timeout: float = 1.0,
) -> None:
    """Yield control to the event loop until ``seen`` is non-empty.

    safe_fire is scheduled via ``loop.create_task`` — the task is not
    awaited by the caller, so we need to yield for at least one loop
    iteration. We poll up to ``timeout`` seconds.
    """
    import asyncio

    deadline = time.monotonic() + timeout
    while not seen and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
