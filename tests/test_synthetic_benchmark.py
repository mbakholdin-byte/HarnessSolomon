"""Tests for harness.eval.synthetic_benchmark — Phase 7.6.

Covers: event count, tier distribution, nonzero tokens, context >= prompt,
complexity keyword correlation, CSV roundtrip, determinism.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from harness.eval.calibration_parser import CSV_COLUMNS
from harness.eval.synthetic_benchmark import SyntheticEvent, generate_synthetic_events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_csv(tmp_path: Path) -> Path:
    """Temporary CSV path for roundtrip tests."""
    return tmp_path / "test_dataset.csv"


@pytest.fixture
def events_2000() -> list:
    """2000 synthetic events (seed=42)."""
    return generate_synthetic_events(n_events=2000, seed=42)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGenerate2000Events:
    """Test 1: generate_synthetic_events returns exactly 2000 events."""

    def test_returns_exact_count(self):
        events = generate_synthetic_events(n_events=2000, seed=42)
        assert len(events) == 2000


class TestTierDistribution:
    """Test 2: tier distribution is approximately T1~50%, T2~30%, T3~20%."""

    def test_tier_distribution_reasonable(self, events_2000):
        total = len(events_2000)
        t1 = sum(1 for e in events_2000 if e.chosen_tier == "t1")
        t2 = sum(1 for e in events_2000 if e.chosen_tier == "t2")
        t3 = sum(1 for e in events_2000 if e.chosen_tier == "t3")

        pct_t1 = t1 / total
        pct_t2 = t2 / total
        pct_t3 = t3 / total

        assert 0.45 <= pct_t1 <= 0.55, f"T1 fraction {pct_t1:.3f} outside [0.45, 0.55]"
        assert 0.25 <= pct_t2 <= 0.35, f"T2 fraction {pct_t2:.3f} outside [0.25, 0.35]"
        assert 0.15 <= pct_t3 <= 0.25, f"T3 fraction {pct_t3:.3f} outside [0.15, 0.25]"


class TestPromptTokensNonzero:
    """Test 3: all prompt_tokens > 0."""

    def test_all_prompt_tokens_positive(self, events_2000):
        for e in events_2000:
            assert e.prompt_tokens > 0, (
                f"prompt_tokens={e.prompt_tokens} for tier={e.chosen_tier}"
            )


class TestContextTokensNonzero:
    """Test 4: all context_tokens > 0 (cumulative)."""

    def test_all_context_tokens_positive(self, events_2000):
        for e in events_2000:
            assert e.context_tokens > 0, (
                f"context_tokens={e.context_tokens} for tier={e.chosen_tier}"
            )


class TestContextGtePromptTokens:
    """Test 5: context_tokens >= prompt_tokens for all events."""

    def test_context_gte_prompt(self, events_2000):
        violations = [
            e for e in events_2000 if e.context_tokens < e.prompt_tokens
        ]
        assert len(violations) == 0, (
            f"{len(violations)} events have context_tokens < prompt_tokens"
        )


class TestComplexityKeywordCorrelation:
    """Test 6: T3 events have >50% complexity keywords."""

    def test_t3_high_keyword_rate(self, events_2000):
        t3_events = [e for e in events_2000 if e.chosen_tier == "t3"]
        kw_count = sum(1 for e in t3_events if e.has_complexity_keyword)
        rate = kw_count / len(t3_events) if t3_events else 0.0
        assert rate > 0.50, (
            f"Only {rate:.1%} of T3 events have complexity keywords (expected >50%)"
        )


class TestCsvRoundtrip:
    """Test 7: write CSV + read back → same data."""

    def test_csv_roundtrip(self, tmp_csv):
        events = generate_synthetic_events(n_events=100, seed=99, output_csv=tmp_csv)
        assert tmp_csv.is_file()

        # Read back using csv.DictReader
        read_back: list[dict] = []
        with open(tmp_csv, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                read_back.append(row)

        assert len(read_back) == 100
        assert len(read_back[0]) == len(CSV_COLUMNS)

        # Spot-check first row
        first = events[0]
        first_row = read_back[0]
        assert first_row["chosen_tier"] == first.chosen_tier
        assert int(first_row["prompt_tokens"]) == first.prompt_tokens
        assert int(first_row["prompt_len_chars"]) == first.prompt_len_chars
        assert int(first_row["context_tokens"]) == first.context_tokens
        assert float(first_row["cost_usd"]) == first.cost_usd


class TestDeterminism:
    """Test 8: same seed → identical events."""

    def test_determinism_seed42(self):
        events_a = generate_synthetic_events(n_events=200, seed=42)
        events_b = generate_synthetic_events(n_events=200, seed=42)

        assert len(events_a) == len(events_b)

        for i, (a, b) in enumerate(zip(events_a, events_b)):
            assert a.ts == b.ts, f"Event {i}: ts differs"
            assert a.prompt_tokens == b.prompt_tokens, f"Event {i}: prompt_tokens differs"
            assert a.prompt_len_chars == b.prompt_len_chars, f"Event {i}: prompt_len_chars differs"
            assert a.context_tokens == b.context_tokens, f"Event {i}: context_tokens differs"
            assert a.chosen_tier == b.chosen_tier, f"Event {i}: chosen_tier differs"
            assert a.has_complexity_keyword == b.has_complexity_keyword, f"Event {i}: keyword differs"
            assert a.cost_usd == b.cost_usd, f"Event {i}: cost_usd differs"
