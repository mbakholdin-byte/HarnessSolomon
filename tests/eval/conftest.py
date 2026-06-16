"""Phase 3 B-mini + Phase 5 B2/B3: shared fixtures for the eval test suite.

Provides:
    - golden_facts (50 facts, uniform distribution: 12 early / 26 mid / 12 late)
    - golden_queries (50 queries, 30 auto + 20 manual) — Phase 5 B2/B3
    - seed_session_100 (100+ turn session, messages padded to 500/800 chars)
    - mock_summariser (AsyncMock that injects all phrases into summary)
    - compactor (ContextCompactor with B6 isolation: store=None, memory=None, etc.)
    - eval_settings (Settings with compaction_threshold_ratio=0.05 override)
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from harness.config import Settings
from harness.context import ContextCompactor
from harness.eval import GoldenFact, GoldenQuery
from harness.eval.golden import load_golden_queries
from harness.server.llm.router import CompletionResult


_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_MANUAL_QUERIES_PATH = _FIXTURES_DIR / "golden_queries.jsonl"


# Golden facts (50, uniformly distributed across the 100-turn session).
# C1 fix: n=50 for statistical reliability on the 95% threshold.
# C2 fix: 12 early (turns 1-30), 26 mid (31-70), 12 late (71-100).
# Phrases are specific (not generic words) so BM25 can lift them.
_GOLDEN_FACT_DATA: list[tuple[str, str, int, str]] = [
    # Early (turns 1-30) - 12 facts
    ("F01", "Phase 3 v1.5.0", 5, "user"),
    ("F02", "Qdrant primary store", 9, "tool_result"),
    ("F03", "Reciprocal Rank Fusion", 13, "user"),
    ("F04", "multilingual-e5-small", 17, "scratchpad"),
    ("F05", "scratchpad L0 hot context", 20, "scratchpad"),
    ("F06", "PreCompact hook fires", 22, "user"),
    ("F07", "T1 Qwen3 8B local", 25, "tool_result"),
    ("F08", "sub-agent v1.0 cascade", 27, "user"),
    ("F09", "WorktreeSession async", 28, "scratchpad"),
    ("F10", "PR webhooks HMAC-SHA256", 14, "tool_result"),
    ("F11", "stacked PR SplitPlanner", 18, "user"),
    ("F12", "auto-merge via gh", 23, "tool_result"),
    # Mid (turns 31-70) - 26 facts
    ("F13", "PrivacyZoneFilter default patterns", 32, "scratchpad"),
    ("F14", "7 default zones private env ssh", 35, "user"),
    ("F15", "match_glob fnmatch translate", 38, "scratchpad"),
    ("F16", "Tier 1 sink read_file grep glob", 41, "user"),
    ("F17", "fail-open filter audit scratchpad", 44, "scratchpad"),
    ("F18", "CompactStore SQLite agent-jobs.db", 47, "user"),
    ("F19", "source_hash sha256 sort_keys", 50, "scratchpad"),
    ("F20", "L2VectorStore Qdrant primary", 53, "tool_result"),
    ("F21", "SQLite fallback make_l2_store", 56, "user"),
    ("F22", "BM25 k1 1.5 b 0.75", 59, "scratchpad"),
    ("F23", "DenseRetriever cosine similarity", 62, "tool_result"),
    ("F24", "HybridRetriever RRF k=60", 65, "user"),
    ("F25", "PrivacyAwareEmbedder redact before embed", 68, "scratchpad"),
    ("F26", "ToolOffloader threshold 25 KB", 33, "user"),
    ("F27", "scratchpad_read_offloaded id query", 36, "scratchpad"),
    ("F28", "ReflectionLoop T1 T2 cascade", 39, "user"),
    ("F29", "SessionLifecycle __aexit__ hook", 42, "scratchpad"),
    ("F30", "force_compact CompactResult dataclass", 45, "user"),
    ("F31", "cache_control ephemeral Anthropic", 48, "tool_result"),
    ("F32", "OutboundWebhookDispatcher httpx", 51, "user"),
    ("F33", "RepoLockRegistry Path resolve", 54, "scratchpad"),
    ("F34", "AdversarialVerify 2 of 3 majority", 57, "user"),
    ("F35", "ContextCompactor sliding window", 60, "scratchpad"),
    ("F36", "keep_recent_turns floor protect", 63, "user"),
    ("F37", "tool-pair preservation contract", 66, "scratchpad"),
    ("F38", "IdentityReranker Phase 1 placeholder", 69, "scratchpad"),
    # Late (turns 71-100) - 12 facts
    ("F39", "Settings 45 to 56 phase 3 v1.5.0", 72, "scratchpad"),
    ("F40", "Scope SESSIONS_WRITE sessions.write", 75, "user"),
    ("F41", "harness sessions compact CLI", 78, "tool_result"),
    ("F42", "POST /api/v1/sessions compact", 81, "user"),
    ("F43", "TimeBasedCompactionTrigger 4 modes", 84, "scratchpad"),
    ("F44", "compaction_trigger hybrid turn time", 87, "scratchpad"),
    ("F45", "force_idle_check kwarg default False", 90, "user"),
    ("F46", "PreCompactState frozen dataclass", 93, "scratchpad"),
    ("F47", "pre-compact-session_id tag namespaced", 96, "scratchpad"),
    ("F48", "asyncio.wait_for per-call timeout", 99, "user"),
    ("F49", "runner.py trust boundary static test", 98, "scratchpad"),
    ("F50", "12 of 12 Phase 3 FINAL closed", 95, "user"),
]


# Auto-generated queries — templates per difficulty bucket.
# Phase 5 B2 fix: queries use the phrase directly (or close paraphrase)
# so BM25 can lift them above generic words. Difficulty controls how
# much of the phrase leaks into the query tokens.
_AUTO_QUERY_TEMPLATES: dict[str, list[str]] = {
    "easy": [
        "what is {phrase}",
        "which feature is {phrase}",
    ],
    "medium": [
        "how is {phrase} configured",
        "explain the {phrase} design",
    ],
    "hard": [
        "describe the architecture of {phrase}",
        "summarise the role of {phrase}",
    ],
}


def _generate_auto_queries(
    facts: list[GoldenFact],
    n: int = 30,
    seed: int = 42,
) -> list[GoldenQuery]:
    """Generate ``n`` auto queries uniformly across difficulty buckets.

    Returns 10 easy + 10 medium + 10 hard (n=30). Each query has
    ``relevant_fact_ids=(fact.id,)`` (single-fact, factual_lookup) and
    4-6 ``irrelevant_fact_ids`` from a different turn region.
    """
    rng = random.Random(seed)
    # Sample 10 facts per bucket. Use turn_index quartiles to spread.
    sorted_facts = sorted(facts, key=lambda f: f.turn_index)
    quartile = len(sorted_facts) // 4
    buckets = {
        "easy": sorted_facts[:quartile],       # turns 5-15 → easy
        "medium": sorted_facts[quartile: 3 * quartile],  # mid turns
        "hard": sorted_facts[3 * quartile:],   # late turns → hard
    }
    queries: list[GoldenQuery] = []
    fact_id_set = {f.id for f in facts}
    qid = 1
    for difficulty, fact_pool in buckets.items():
        # Take 10 from each bucket (or all if less).
        sample = rng.sample(fact_pool, min(10, len(fact_pool)))
        templates = _AUTO_QUERY_TEMPLATES[difficulty]
        for i, fact in enumerate(sample):
            template = templates[i % len(templates)]
            query_text = template.format(phrase=fact.phrase)
            # Pick 4-6 irrelevant fact_ids from a different region.
            other_pool = [f for f in facts if f.id != fact.id]
            n_irrelevant = rng.randint(4, min(6, len(other_pool)))
            irrelevant = rng.sample(other_pool, n_irrelevant)
            irrelevant_ids = tuple(f.id for f in irrelevant)
            queries.append(GoldenQuery(
                id=f"AUTO-{qid:02d}",
                query=query_text,
                relevant_fact_ids=(fact.id,),
                irrelevant_fact_ids=irrelevant_ids,
                category="factual_lookup",
                difficulty=difficulty,
            ))
            qid += 1
    return queries


@pytest.fixture
def golden_facts() -> list[GoldenFact]:
    """50 marked facts uniformly distributed across the 100-turn session."""
    return [
        GoldenFact(id=fid, phrase=phrase, turn_index=tidx, category=cat)
        for fid, phrase, tidx, cat in _GOLDEN_FACT_DATA
    ]


@pytest.fixture
def golden_queries(golden_facts: list[GoldenFact]) -> list[GoldenQuery]:
    """50 golden queries: 30 auto-generated + 20 manual from JSONL.

    Phase 5 B2/B3 DoD set:
      - 30 auto (10 easy + 10 medium + 10 hard, factual_lookup only)
      - 10 manual factual_lookup
      - 5 manual paraphrased
      - 5 manual multi_hop

    Threshold scope: precision@5 / recall@20 on the **subset** of
    40 factual_lookup + paraphrased queries (multi_hop reported
    separately per docs/PHASE5-B2-B3-PLAN.md §5.1 + sign-off 2026-06-16).
    """
    auto = _generate_auto_queries(golden_facts, n=30, seed=42)
    manual = load_golden_queries(_MANUAL_QUERIES_PATH)
    return auto + manual


@pytest.fixture
def eval_settings() -> Settings:
    """Settings tuned for the 100-turn B-mini golden session.

    B4 fix: explicit override of the 0.75 default to 0.05 so the
    100-turn session (32K tokens) crosses the threshold (1.6K tokens)
    by 20x - no flake.
    R6 fix: compaction_persist_to_memory=False to keep test isolation
    (no L2 writes against production data/agent-jobs.db).
    LLM-always-called fix: compaction_target_ratio=0.001 (32 tokens).
    Even after the sliding window trims to 5 messages (~791 tokens),
    the target of 32 tokens forces the LLM summariser to run,
    producing a real summary for the B1/B4 metrics to inspect.
    Note: compactor uses ``compaction_threshold_ratio`` for BOTH
    threshold and target checks (compaction.py:379-380), so we set
    target < threshold to satisfy the validator while still being
    much smaller than the trimmed output.
    """
    return Settings(
        compaction_enabled=True,
        compaction_threshold_ratio=0.005,
        compaction_target_ratio=0.001,
        compaction_keep_recent_turns=4,
        compaction_summarizer_max_input_tokens=4000,
        compaction_persist_to_memory=False,
    )


def _pad(text: str, target_chars: int) -> str:
    """Pad text with deterministic filler to ``target_chars`` characters."""
    if len(text) >= target_chars:
        return text[:target_chars]
    filler = " (padding for token-estimate reliability)"
    while len(text) < target_chars:
        text += filler
    return text[:target_chars]


@pytest.fixture
def seed_session_100(
    golden_facts: list[GoldenFact],
) -> list[dict[str, Any]]:
    """A 100+ turn session with golden facts woven in.

    R1 fix: pad each message to known char count (500 chars/user,
    800 chars/assistant) so the token estimate is well above the
    threshold with margin. Total: ~200 messages, ~130K chars, ~32K tokens.
    """
    user_messages_target = 100  # 100 user + 100 assistant + 5 tool = 205
    fact_by_turn: dict[int, GoldenFact] = {f.turn_index: f for f in golden_facts}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _pad("You are Solomon Harness assistant.", 400)},
    ]
    for i in range(user_messages_target):
        user_text = f"User turn {i}: question about Phase 3 context engineering"
        if i in fact_by_turn:
            fact = fact_by_turn[i]
            user_text += f" - includes fact: {fact.phrase}"
        messages.append({"role": "user", "content": _pad(user_text, 500)})
        messages.append({
            "role": "assistant",
            "content": _pad(f"Assistant turn {i}: ack and continue.", 800),
        })
    # Add 5 tool turns to exercise tool-pair preservation (B5).
    for j in range(5):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"call_{j}", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"call_{j}",
            "content": _pad(f"Tool result {j}: file contents.", 400),
        })
    return messages


@pytest.fixture
def mock_summariser(golden_facts: list[GoldenFact]) -> "LLMRouter":
    """Fake ``LLMRouter`` subclass that injects ALL phrases into the summary.

    The real ``LLMRouter.completion`` is async, calls
    ``_call_litellm_completion`` (the litellm layer), then
    ``_normalize_completion`` (litellm.ModelResponse -> CompletionResult).
    AsyncMock + side_effect doesn't unwrap correctly in compactor's
    ``await self._router.completion(...)`` path (the mock returns a
    coroutine that's never awaited inside compactor's own call chain
    when the mock is shaped like a router).

    Solution: subclass ``LLMRouter`` and override the two private
    methods. ``completion`` itself is inherited and works normally.
    """
    from harness.server.llm.router import LLMRouter

    class _FakeLLMRouter(LLMRouter):
        async def _call_litellm_completion(self, model, messages, **kwargs):
            # Return a sentinel — _normalize_completion ignores the
            # response and just builds the desired CompletionResult.
            return None

        def _normalize_completion(self, model, response):
            phrases = "\n".join(f"- {f.phrase}" for f in golden_facts)
            body = (
                "[Compaction summary - earlier turns condensed]\n"
                f"Preserved facts:\n{phrases}\n"
            )
            return CompletionResult(content=body, model=model, usage={})

    return _FakeLLMRouter.__new__(_FakeLLMRouter)


@pytest.fixture
def compactor(
    eval_settings: Settings,
    mock_summariser: AsyncMock,
) -> ContextCompactor:
    """ContextCompactor wired with the mock summariser and B6 isolation.

    B6 fix: store=None, memory=None, audit=None, pre_compact_hook=None,
    idle_trigger=None - no L2 writes, no DB writes, no audit I/O.
    """
    return ContextCompactor(
        eval_settings,
        mock_summariser,  # type: ignore[arg-type]
        memory=None,
        session_id="b-mini-test",
        store=None,
        audit=None,
        pre_compact_hook=None,
        idle_trigger=None,
    )
