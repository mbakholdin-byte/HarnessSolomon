"""Phase 7.6 v1.34.0 — Regression tests for recalibrated tier router thresholds.

Verifies:

  1. ``tier_routing_t1_max_context_tokens`` == 2000 (v1.34.0 default)
  2. ``tier_routing_t3_min_prompt_chars`` == 10000 (v1.34.0 default)
  3. Existing tier tests still pass with new defaults
  4. Calibration v2 tests still pass
  5. CHANGELOG contains v1.34.0 section

Run::

    pytest tests/test_phase76_regression.py -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness.config import settings

# ---------------------------------------------------------------------------
# Test 1–2: Settings values match v1.34.0 calibration
# ---------------------------------------------------------------------------


def test_settings_t1_context_matches_v134() -> None:
    """``tier_routing_t1_max_context_tokens`` default is 2000 (v1.34.0)."""
    assert settings.tier_routing_t1_max_context_tokens == 2000, (
        f"Expected t1_max_context_tokens=2000, "
        f"got {settings.tier_routing_t1_max_context_tokens}"
    )


def test_settings_t3_prompt_matches_v134() -> None:
    """``tier_routing_t3_min_prompt_chars`` default is 10000 (v1.34.0)."""
    assert settings.tier_routing_t3_min_prompt_chars == 10000, (
        f"Expected t3_min_prompt_chars=10000, "
        f"got {settings.tier_routing_t3_min_prompt_chars}"
    )


# ---------------------------------------------------------------------------
# Test 3: Existing tier tests still pass
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_existing_tier_tests_still_pass() -> None:
    """``test_tier_selector_v126.py`` passes with v1.34.0 defaults."""
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            str(_REPO_ROOT / "tests" / "test_tier_selector_v126.py"),
            "-v", "--tb=short",
            "-p", "no:cacheprovider",
            "--no-header",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
    )
    # pytest returns non-zero on test failures; we expect 0 (all pass).
    assert result.returncode == 0, (
        f"test_tier_selector_v126.py FAILED (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout[-2000:]}\n"
        f"STDERR:\n{result.stderr[-2000:]}"
    )


# ---------------------------------------------------------------------------
# Test 4: Calibration v2 tests still pass
# ---------------------------------------------------------------------------


def test_calibration_v2_tests_pass() -> None:
    """``test_synthetic_benchmark.py`` + ``test_calibration_report.py`` pass."""
    for test_file in ("test_synthetic_benchmark.py", "test_calibration_report.py"):
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(_REPO_ROOT / "tests" / test_file),
                "-v", "--tb=short",
                "-p", "no:cacheprovider",
                "--no-header",
            ],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=60,
        )
        assert result.returncode == 0, (
            f"{test_file} FAILED (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout[-2000:]}\n"
            f"STDERR:\n{result.stderr[-2000:]}"
        )


# ---------------------------------------------------------------------------
# Test 5: CHANGELOG contains v1.34.0 section
# ---------------------------------------------------------------------------


def test_changelog_has_v134_section() -> None:
    """``docs/CHANGELOG.md`` contains the ``[1.34.0]`` section header."""
    changelog_path = _REPO_ROOT / "docs" / "CHANGELOG.md"
    text = changelog_path.read_text(encoding="utf-8")
    assert "## [1.34.0]" in text, (
        "CHANGELOG.md does not contain '## [1.34.0]' section header"
    )
    # Also verify the key threshold changes are mentioned.
    assert "t1_max_context_tokens 8000→2000" in text, (
        "CHANGELOG missing t1_max_context_tokens threshold change"
    )
    assert "t3_min_prompt_chars 3000→10000" in text, (
        "CHANGELOG missing t3_min_prompt_chars threshold change"
    )
