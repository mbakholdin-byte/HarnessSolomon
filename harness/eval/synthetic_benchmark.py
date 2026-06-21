"""Phase 7.6: Synthetic benchmark for Tier Router calibration.

Generates realistic LLM usage events with varying prompt lengths,
context sizes, and tool-call patterns. Used to produce golden
dataset v2 for threshold calibration.

**Trust boundary:** stdlib only (csv, json, logging, random, dataclasses,
pathlib). Imports ``RoutingEvent``, ``CSV_COLUMNS`` from
:mod:`harness.eval.calibration_parser`. NO imports from
``harness.agents``, ``harness.server``, or ``harness.context``.
"""
from __future__ import annotations

import csv
import json
import logging
import random
from dataclasses import asdict, dataclass
from pathlib import Path

from harness.eval.calibration_parser import CSV_COLUMNS, RoutingEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synthetic event data model (internal)
# ---------------------------------------------------------------------------

#: Model pools for each tier.
_MODELS_T1: list[str] = ["qwen3:8b", "llama3.2:3b", "phi4:mini"]
_MODELS_T2: list[str] = ["qwen2.5:14b", "llama3.1:8b", "mistral:7b"]
_MODELS_T3: list[str] = ["claude-sonnet-4-20250514", "minimax-m2", "gpt-4o"]

#: Cost per tier (USD per call).
_COST_MAP: dict[str, float] = {"t1": 0.001, "t2": 0.005, "t3": 0.020}

#: Session ID pool.
_SESSION_COUNT: int = 40


@dataclass
class SyntheticEvent:
    """Internal representation of a synthetic LLM usage event.

    All 13 public fields map directly to :class:`RoutingEvent` columns.
    """

    ts: str
    session_id: str
    prompt_len_chars: int
    prompt_tokens: int
    context_tokens: int
    has_tool_calls: bool
    has_complexity_keyword: bool
    confidence: float
    chosen_tier: str
    actual_model: str
    status: str
    error_class: str | None
    cost_usd: float

    def to_routing_event(self) -> RoutingEvent:
        """Convert to :class:`RoutingEvent` for calibration pipeline."""
        return RoutingEvent(
            ts=self.ts,
            session_id=self.session_id,
            prompt_len_chars=self.prompt_len_chars,
            prompt_tokens=self.prompt_tokens,
            context_tokens=self.context_tokens,
            has_tool_calls=self.has_tool_calls,
            has_complexity_keyword=self.has_complexity_keyword,
            confidence=self.confidence,
            chosen_tier=self.chosen_tier,
            actual_model=self.actual_model,
            status=self.status,
            error_class=self.error_class,
            cost_usd=self.cost_usd,
        )


# ---------------------------------------------------------------------------
# Per-tier generators
# ---------------------------------------------------------------------------


def _gen_t1(rng: random.Random, idx: int, sessions: list[str]) -> SyntheticEvent:
    """Generate a T1 (short, simple) event.

    Prompt: 100–1500 chars → 25–375 tokens.
    Context: prompt_tokens + 0–2000 prev-turn tokens.
    No complexity keywords. 90% status=ok.
    """
    prompt_len = rng.randint(100, 1500)
    prompt_tokens = _chars_to_tokens(prompt_len, rng)
    prev_completion = rng.randint(0, 2000)
    context_tokens = prompt_tokens + prev_completion

    has_tool = rng.random() < 0.05
    has_kw = False
    confidence = round(rng.uniform(0.70, 0.90), 4)
    status = "error" if rng.random() < 0.10 else "ok"
    error = "timeout" if status == "error" and rng.random() < 0.5 else None

    return SyntheticEvent(
        ts=_make_ts(idx, rng),
        session_id=rng.choice(sessions),
        prompt_len_chars=prompt_len,
        prompt_tokens=prompt_tokens,
        context_tokens=context_tokens,
        has_tool_calls=has_tool,
        has_complexity_keyword=has_kw,
        confidence=confidence,
        chosen_tier="t1",
        actual_model=rng.choice(_MODELS_T1),
        status=status,
        error_class=error,
        cost_usd=_COST_MAP["t1"],
    )


def _gen_t2(rng: random.Random, idx: int, sessions: list[str]) -> SyntheticEvent:
    """Generate a T2 (medium, mixed) event.

    Prompt: 500–3000 chars → 125–750 tokens.
    Context: prompt_tokens + 200–8000 prev-turn tokens.
    10% complexity keywords. 30% tool calls. 90% status=ok.
    """
    prompt_len = rng.randint(500, 3000)
    prompt_tokens = _chars_to_tokens(prompt_len, rng)
    prev_completion = rng.randint(200, 8000)
    context_tokens = prompt_tokens + prev_completion

    has_tool = rng.random() < 0.30
    has_kw = rng.random() < 0.10
    confidence = round(rng.uniform(0.60, 0.80), 4)
    status = "error" if rng.random() < 0.10 else "ok"
    error = "timeout" if status == "error" and rng.random() < 0.5 else None

    return SyntheticEvent(
        ts=_make_ts(idx, rng),
        session_id=rng.choice(sessions),
        prompt_len_chars=prompt_len,
        prompt_tokens=prompt_tokens,
        context_tokens=context_tokens,
        has_tool_calls=has_tool,
        has_complexity_keyword=has_kw,
        confidence=confidence,
        chosen_tier="t2",
        actual_model=rng.choice(_MODELS_T2),
        status=status,
        error_class=error,
        cost_usd=_COST_MAP["t2"],
    )


def _gen_t3(rng: random.Random, idx: int, sessions: list[str]) -> SyntheticEvent:
    """Generate a T3 (complex) event.

    Prompt: 2000–10000 chars → 500–2500 tokens.
    Context: prompt_tokens + 500–32000 prev-turn tokens.
    80% complexity keywords. 60% tool calls. 90% status=ok.
    """
    prompt_len = rng.randint(2000, 10000)
    prompt_tokens = _chars_to_tokens(prompt_len, rng)
    prev_completion = rng.randint(500, 32000)
    context_tokens = prompt_tokens + prev_completion

    has_tool = rng.random() < 0.60
    has_kw = rng.random() < 0.80
    confidence = round(rng.uniform(0.40, 0.70), 4)
    status = "error" if rng.random() < 0.10 else "ok"
    error = "timeout" if status == "error" and rng.random() < 0.5 else None

    return SyntheticEvent(
        ts=_make_ts(idx, rng),
        session_id=rng.choice(sessions),
        prompt_len_chars=prompt_len,
        prompt_tokens=prompt_tokens,
        context_tokens=context_tokens,
        has_tool_calls=has_tool,
        has_complexity_keyword=has_kw,
        confidence=confidence,
        chosen_tier="t3",
        actual_model=rng.choice(_MODELS_T3),
        status=status,
        error_class=error,
        cost_usd=_COST_MAP["t3"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Base timestamp (2026-06-15 00:00 UTC) for event ordering.
_BASE_TS: float = 1781510400.0


def _make_ts(idx: int, rng: random.Random) -> str:
    """Generate a monotonically increasing timestamp string."""
    ts = _BASE_TS + idx * 10.0 + rng.uniform(0.0, 5.0)
    return f"{ts:.6f}"


def _chars_to_tokens(chars: int, rng: random.Random) -> int:
    """Convert chars to tokens with ±20% noise (avg 4 chars/token)."""
    base = chars / 4.0
    noise = rng.uniform(-0.20, 0.20)
    raw = base * (1.0 + noise)
    return max(1, round(raw))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_synthetic_events(
    n_events: int = 2000,
    seed: int = 42,
    output_csv: Path | None = None,
    output_jsonl: Path | None = None,
) -> list[RoutingEvent]:
    """Generate N synthetic LLM usage events.

    Distribution:
        - T1 (short, simple):     50% — prompt 100–1500 chars, 25–375 tokens,
          no complexity keywords, 5% tool calls.
        - T2 (medium, mixed):     30% — prompt 500–3000 chars, 125–750 tokens,
          10% keywords, 30% tool calls.
        - T3 (complex):           20% — prompt 2000–10000 chars, 500–2500 tokens,
          80% keywords, 60% tool calls.

    Context tokens simulate multi-turn: ``context_tokens = prompt_tokens +
    random_prev_completion``, so ``context_tokens >= prompt_tokens`` always.

    Args:
        n_events: Total events to generate.
        seed: Random seed for reproducibility.
        output_csv: If provided, writes a 13-column CSV to this path.
        output_jsonl: If provided, writes JSONL (one JSON object per line).

    Returns:
        List of :class:`RoutingEvent` objects, compatible with
        :func:`harness.eval.calibration_report.read_golden_dataset`.
    """
    rng = random.Random(seed)

    # Session pool
    sessions = [f"synth-session-{i:04d}" for i in range(_SESSION_COUNT)]

    # Calculate tier counts
    n_t1 = int(n_events * 0.50)
    n_t2 = int(n_events * 0.30)
    n_t3 = n_events - n_t1 - n_t2  # remainder to hit exact total

    logger.info(
        "generating %d synthetic events: t1=%d t2=%d t3=%d (seed=%d)",
        n_events, n_t1, n_t2, n_t3, seed,
    )

    synthetic: list[SyntheticEvent] = []

    for i in range(n_t1):
        synthetic.append(_gen_t1(rng, i, sessions))
    for i in range(n_t2):
        synthetic.append(_gen_t2(rng, n_t1 + i, sessions))
    for i in range(n_t3):
        synthetic.append(_gen_t3(rng, n_t1 + n_t2 + i, sessions))

    # Shuffle so tier order is not strictly T1→T2→T3
    rng.shuffle(synthetic)

    # Convert to RoutingEvent
    events = [s.to_routing_event() for s in synthetic]

    # Write outputs
    if output_csv is not None:
        _write_csv(events, output_csv)
    if output_jsonl is not None:
        _write_jsonl(synthetic, output_jsonl)

    # Summary
    t1_count = sum(1 for e in events if e.chosen_tier == "t1")
    t2_count = sum(1 for e in events if e.chosen_tier == "t2")
    t3_count = sum(1 for e in events if e.chosen_tier == "t3")
    kw_pct = sum(1 for e in events if e.has_complexity_keyword) / len(events) * 100
    zero_ctx = sum(1 for e in events if e.context_tokens == 0)

    logger.info(
        "synthetic benchmark generated: %d events (t1=%d, t2=%d, t3=%d), "
        "%.1f%% with complexity keywords, %d with context_tokens=0",
        len(events), t1_count, t2_count, t3_count, kw_pct, zero_ctx,
    )

    return events


def _write_csv(events: list[RoutingEvent], path: Path) -> None:
    """Write routing events as a 13-column CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for event in events:
            row = {col: getattr(event, col) for col in CSV_COLUMNS}
            writer.writerow(row)
    logger.info("CSV written: %d rows → %s", len(events), path)


def _write_jsonl(synthetic: list[SyntheticEvent], path: Path) -> None:
    """Write synthetic events as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for s in synthetic:
            fh.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
    logger.info("JSONL written: %d lines → %s", len(synthetic), path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "SyntheticEvent",
    "generate_synthetic_events",
]
