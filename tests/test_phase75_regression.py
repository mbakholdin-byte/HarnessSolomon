"""Phase 7.5 v1.33.0 — Regression tests for calibrated Tier Router thresholds.

Ensures:
  1. All 7 config defaults match the recommended calibrated values.
  2. Existing tier selector tests still pass with new defaults.
  3. Existing cascade tests still pass with new defaults.
  4. All 3 calibration test files pass.
  5. CHANGELOG contains the v1.33.0 section.

Run::

    pytest tests/test_phase75_regression.py -v --tb=short
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.config import settings

# ---------------------------------------------------------------------------
# Recommended calibrated values (Phase 7.5)
# ---------------------------------------------------------------------------

RECOMMENDED = {
    "subagent_confidence_high": 0.60,
    "subagent_confidence_low": 0.30,
    "tier_routing_t1_max_prompt_chars": 1000,
    "tier_routing_t1_max_context_tokens": 8000,
    "tier_routing_t3_min_prompt_chars": 3000,
    "tier_routing_t3_min_context_tokens": 16000,
    "tier_routing_complexity_keywords": (
        "reasoning", "analyze", "prove", "derive", "evaluate",
    ),
}

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTEST_ARGS = ["python", "-m", "pytest", "-v", "--tb=short", "--no-header"]


# ---------------------------------------------------------------------------
# Test 1: defaults match recommended
# ---------------------------------------------------------------------------


class TestDefaultsMatchRecommended:
    """All 7 config defaults must equal the Phase 7.5 calibrated values."""

    def test_subagent_confidence_high(self) -> None:
        assert settings.subagent_confidence_high == pytest.approx(
            RECOMMENDED["subagent_confidence_high"]
        ), (
            f"Expected {RECOMMENDED['subagent_confidence_high']}, "
            f"got {settings.subagent_confidence_high}"
        )

    def test_subagent_confidence_low(self) -> None:
        assert settings.subagent_confidence_low == pytest.approx(
            RECOMMENDED["subagent_confidence_low"]
        ), (
            f"Expected {RECOMMENDED['subagent_confidence_low']}, "
            f"got {settings.subagent_confidence_low}"
        )

    def test_t1_max_prompt_chars(self) -> None:
        assert (
            settings.tier_routing_t1_max_prompt_chars
            == RECOMMENDED["tier_routing_t1_max_prompt_chars"]
        )

    def test_t1_max_context_tokens(self) -> None:
        assert (
            settings.tier_routing_t1_max_context_tokens
            == RECOMMENDED["tier_routing_t1_max_context_tokens"]
        )

    def test_t3_min_prompt_chars(self) -> None:
        assert (
            settings.tier_routing_t3_min_prompt_chars
            == RECOMMENDED["tier_routing_t3_min_prompt_chars"]
        )

    def test_t3_min_context_tokens(self) -> None:
        assert (
            settings.tier_routing_t3_min_context_tokens
            == RECOMMENDED["tier_routing_t3_min_context_tokens"]
        )

    def test_complexity_keywords(self) -> None:
        expected = list(RECOMMENDED["tier_routing_complexity_keywords"])
        actual = list(settings.tier_routing_complexity_keywords)
        assert actual == expected, (
            f"Expected complexity_keywords {expected}, got {actual}"
        )


# ---------------------------------------------------------------------------
# Test 2: existing tier selector tests still pass
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_existing_tier_selector_tests_still_pass() -> None:
    """Run test_tier_selector_v126.py and assert it passes."""
    result = subprocess.run(
        [*PYTEST_ARGS, str(REPO_ROOT / "tests" / "test_tier_selector_v126.py")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"test_tier_selector_v126.py FAILED:\n"
        f"--- STDOUT ---\n{result.stdout[-2000:]}\n"
        f"--- STDERR ---\n{result.stderr[-1000:]}"
    )


# ---------------------------------------------------------------------------
# Test 3: existing cascade tests still pass
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_existing_cascade_tests_still_pass() -> None:
    """Run test_agent_cascade.py and assert it passes."""
    result = subprocess.run(
        [*PYTEST_ARGS, str(REPO_ROOT / "tests" / "test_agent_cascade.py")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"test_agent_cascade.py FAILED:\n"
        f"--- STDOUT ---\n{result.stdout[-2000:]}\n"
        f"--- STDERR ---\n{result.stderr[-1000:]}"
    )


# ---------------------------------------------------------------------------
# Test 4: calibration test suite passes
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_calibration_test_suite_passes() -> None:
    """Run all 3 calibration test files and assert they pass."""
    calib_files = [
        "tests/test_calibration_parser.py",
        "tests/test_threshold_grid_search.py",
        "tests/test_calibration_report.py",
    ]
    result = subprocess.run(
        [*PYTEST_ARGS, *(str(REPO_ROOT / f) for f in calib_files)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"Calibration test suite FAILED:\n"
        f"--- STDOUT ---\n{result.stdout[-2000:]}\n"
        f"--- STDERR ---\n{result.stderr[-1000:]}"
    )


# ---------------------------------------------------------------------------
# Test 5: CHANGELOG has v1.33.0 section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("required_text", [
    "[1.33.0]",
    "2026-06-21",
    "Tier Router thresholds calibrated",
    "7 heuristic parameters tuned",
    "Wider T1 zone",
    "Lower confidence thresholds",
])
def test_changelog_has_v133_section(required_text: str) -> None:
    """CHANGELOG must contain the v1.33.0 section with key details."""
    changelog_path = REPO_ROOT / "docs" / "CHANGELOG.md"
    assert changelog_path.is_file(), f"CHANGELOG not found at {changelog_path}"
    content = changelog_path.read_text(encoding="utf-8")
    assert required_text in content, (
        f"CHANGELOG missing expected text: {required_text!r}"
    )
