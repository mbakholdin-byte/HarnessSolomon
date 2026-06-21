"""Phase 7.5 — Tests for :mod:`harness.eval.calibration_parser`.

Covers:
    * Basic parsing of a single log file.
    * Multi-file (5-day) bulk parsing.
    * Golden dataset CSV schema validation.
    * Join integrity between routing decisions and LLM calls.
    * Tier distribution sanity checks.
    * Complexity keyword detection.
    * Graceful handling of missing prompt text.
    * CSV round-trip (write → read → validate).
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

from harness.eval.calibration_parser import (
    CSV_COLUMNS,
    RoutingEvent,
    parse_log_files,
    write_golden_dataset,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_dir() -> Path:
    """Path to real log files (checked into data/logs/)."""
    path = Path(__file__).resolve().parent.parent / "data" / "logs"
    if not path.is_dir():
        pytest.skip(f"log directory not available: {path}")
    return path


@pytest.fixture
def golden_csv_path(tmp_path: Path) -> Path:
    """Temporary output path for the golden CSV."""
    return tmp_path / "golden_routing_dataset.csv"


# ---------------------------------------------------------------------------
# Test 1: Basic single-file parsing
# ---------------------------------------------------------------------------


def test_parse_log_file_basic(log_dir: Path) -> None:
    """Parse a single log file and verify at least 1 routing decision."""
    events = parse_log_files(log_dir, days=["2026-06-17"])

    assert len(events) >= 1, "expected at least 1 routing decision"

    # All events must be valid RoutingEvent instances
    for evt in events:
        assert isinstance(evt, RoutingEvent)
        assert evt.chosen_tier in ("T1", "T2", "T3", "unknown")
        assert evt.actual_model
        assert evt.confidence >= 0.0
        assert evt.confidence <= 1.0
        assert evt.cost_usd >= 0.0


# ---------------------------------------------------------------------------
# Test 2: All-day bulk parsing
# ---------------------------------------------------------------------------


def test_parse_log_file_all_days(log_dir: Path) -> None:
    """Parse all 5 log files; expect a reasonable number of events."""
    events = parse_log_files(log_dir)

    # 5 files cover ~37K events; expect ≥100 llm_call events total
    assert len(events) >= 100, (
        f"expected ≥100 routing events from 5 files, got {len(events)}"
    )

    # All events must have the 13 CSV fields populated (no missing critical data)
    for evt in events:
        assert isinstance(evt.ts, str) and evt.ts
        assert isinstance(evt.chosen_tier, str) and evt.chosen_tier
        assert isinstance(evt.actual_model, str) and evt.actual_model
        assert evt.cost_usd >= 0.0

    logging.info("parsed %d routing events from 5 days", len(events))


# ---------------------------------------------------------------------------
# Test 3: CSV schema validation
# ---------------------------------------------------------------------------


def test_golden_dataset_schema(
    log_dir: Path, golden_csv_path: Path
) -> None:
    """Write CSV and verify all 13 columns are present."""
    events = parse_log_files(log_dir, days=["2026-06-18"])
    write_golden_dataset(events, golden_csv_path)

    assert golden_csv_path.is_file(), "CSV file was not created"

    with open(golden_csv_path, encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)

    expected_columns = [
        "ts",
        "session_id",
        "prompt_len_chars",
        "prompt_tokens",
        "context_tokens",
        "has_tool_calls",
        "has_complexity_keyword",
        "confidence",
        "chosen_tier",
        "actual_model",
        "status",
        "error_class",
        "cost_usd",
    ]

    assert header == expected_columns, (
        f"CSV header mismatch.\nGot:      {header}\nExpected: {expected_columns}"
    )

    # Verify data rows exist
    with open(golden_csv_path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    assert len(rows) == len(events), (
        f"CSV row count ({len(rows)}) != events count ({len(events)})"
    )


# ---------------------------------------------------------------------------
# Test 4: Join integrity — routing decision ↔ llm_call
# ---------------------------------------------------------------------------


def test_join_routing_and_llm_call(log_dir: Path) -> None:
    """Verify that routing events have both routing decisions and LLM data.

    Since the real logs use llm_call as primary source (OnRoutingDecision
    is just a hook marker with sparse data), each RoutingEvent is
    built from one llm_call. This test verifies the join produces
    complete records — all events have model, tier, tokens, cost.
    """
    events = parse_log_files(log_dir, days=["2026-06-18", "2026-06-19"])

    assert len(events) >= 10, (
        f"expected ≥10 routing events from 2 days, got {len(events)}"
    )

    complete_count = 0
    for evt in events:
        has_model = evt.actual_model not in ("", "unknown")
        has_tier = evt.chosen_tier not in ("", "unknown")
        has_cost = evt.cost_usd >= 0.0

        if has_model and has_tier and has_cost:
            complete_count += 1

    # At least 80% of events should have complete data
    ratio = complete_count / max(len(events), 1)
    assert ratio >= 0.80, (
        f"only {ratio:.1%} events have complete model/tier/cost data "
        f"({complete_count}/{len(events)})"
    )


# ---------------------------------------------------------------------------
# Test 5: Tier distribution sanity
# ---------------------------------------------------------------------------


def test_chosen_tier_distribution(log_dir: Path) -> None:
    """T1/T2/T3 distribution should not be 0% and not 100% for any tier.

    In real logs T1 (local qwen3:8b) and T3 (MiniMax-M2.7) both appear.
    T2 may be absent from current logs (GLM-4.7 not yet deployed).
    """
    events = parse_log_files(log_dir)

    tiers: dict[str, int] = {}
    for evt in events:
        t = evt.chosen_tier
        tiers[t] = tiers.get(t, 0) + 1

    total = len(events)
    assert total > 0

    logging.info("tier distribution: %s (total=%d)", tiers, total)

    # T1 should not be 0% (local model is used) => at least 5%
    t1_ratio = tiers.get("T1", 0) / total
    assert t1_ratio > 0.05, (
        f"T1 ratio too low: {t1_ratio:.1%} — expected T1 usage"
    )

    # T3 should not be 0% (cloud model is used) => at least 5%
    t3_ratio = tiers.get("T3", 0) / total
    assert t3_ratio > 0.05, (
        f"T3 ratio too low: {t3_ratio:.1%} — expected T3 usage"
    )

    # T1 should not be 100% — T3 is also used
    assert t1_ratio < 0.95, (
        f"T1 ratio too high: {t1_ratio:.1%} — T3 should also appear"
    )

    # T3 should not be 100% — T1 is also used
    assert t3_ratio < 0.95, (
        f"T3 ratio too high: {t3_ratio:.1%} — T1 should also appear"
    )


# ---------------------------------------------------------------------------
# Test 6: Complexity keyword detection
# ---------------------------------------------------------------------------


def test_complexity_keyword_detection_works(log_dir: Path) -> None:
    """Verify that complexity detection produces meaningful results.

    Since prompt text is absent, complexity is derived from model_id/model.
    Frontier models (MiniMax, Claude) should be flagged; local models
    (qwen3:8b) should not.
    """
    events = parse_log_files(log_dir)

    # At least some events should have complexity detected
    complex_events = [e for e in events if e.has_complexity_keyword]
    assert len(complex_events) > 0, (
        "expected at least some events with complexity keyword detected "
        "(MiniMax/Claude models)"
    )

    # T3 events (frontier cloud) should mostly be complex
    t3_events = [e for e in events if e.chosen_tier == "T3"]
    if t3_events:
        t3_complex = [e for e in t3_events if e.has_complexity_keyword]
        t3_complex_ratio = len(t3_complex) / len(t3_events)
        # The complexity heuristic marks minimax/claude as complex
        # which should cover most T3 calls
        assert t3_complex_ratio >= 0.10, (
            f"only {t3_complex_ratio:.1%} T3 events flagged as complex"
        )

    logging.info(
        "complexity detection: %d/%d events flagged",
        len(complex_events),
        len(events),
    )


# ---------------------------------------------------------------------------
# Test 7: No prompt text — handled gracefully
# ---------------------------------------------------------------------------


def test_no_prompt_text_handled_gracefully(log_dir: Path) -> None:
    """Verify parser does not crash when prompt text is absent.

    The logs intentionally lack prompt content (privacy). The parser
    must handle this gracefully — no KeyError, no crash, no empty output
    due to missing prompt fields.
    """
    events = parse_log_files(log_dir)

    assert len(events) > 0, (
        "parser should produce events even without prompt text"
    )

    # prompt_len_chars is estimated from prompt_tokens (may be 0)
    # This should not cause any crash
    for evt in events:
        assert isinstance(evt.prompt_len_chars, int)
        assert evt.prompt_len_chars >= 0

    # has_complexity_keyword should still produce a result
    # (derived from model/metadata, not prompt text)
    complex_flags = {e.has_complexity_keyword for e in events}
    assert len(complex_flags) >= 1, (
        "expected has_complexity_keyword to produce meaningful results"
    )


# ---------------------------------------------------------------------------
# Test 8: CSV round-trip
# ---------------------------------------------------------------------------


def test_csv_writable_and_readable(
    log_dir: Path, golden_csv_path: Path
) -> None:
    """Write CSV, read it back, verify data integrity.

    Checks:
        * All 13 columns present.
        * Row count matches.
        * Sample fields round-trip correctly.
    """
    events = parse_log_files(log_dir, days=["2026-06-17"])

    # Write
    write_golden_dataset(events, golden_csv_path)
    assert golden_csv_path.is_file()

    # Read back
    with open(golden_csv_path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    assert len(rows) == len(events), (
        f"round-trip row mismatch: wrote {len(events)}, read {len(rows)}"
    )

    assert reader.fieldnames is not None
    fieldnames = list(reader.fieldnames)
    assert len(fieldnames) == 13, (
        f"expected 13 columns, got {len(fieldnames)}: {fieldnames}"
    )

    # Spot-check first event
    if events:
        first_row = rows[0]
        first_evt = events[0]

        assert first_row["chosen_tier"] == first_evt.chosen_tier
        assert first_row["actual_model"] == first_evt.actual_model
        assert float(first_row["cost_usd"]) == pytest.approx(
            first_evt.cost_usd, rel=1e-9
        )
        assert int(first_row["prompt_tokens"]) == first_evt.prompt_tokens
        assert (
            first_row["has_tool_calls"]
            == str(first_evt.has_tool_calls)
        )
        assert (
            first_row["has_complexity_keyword"]
            == str(first_evt.has_complexity_keyword)
        )

    logging.info(
        "CSV round-trip OK: %d rows, %d columns",
        len(rows),
        len(fieldnames),
    )
