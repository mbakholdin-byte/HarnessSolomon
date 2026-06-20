"""WI-08 — Trust Boundary Tests + Build Smoke for web/ admin UI.

Validates that the web/ TypeScript frontend has no dependency on the
Python harness/ layer and that the build pipeline is healthy.

Tests:
  1. AST no harness imports — scan web/src/**/*.ts* for import of "harness"
  2. Build smoke — npm ci + npm run build (slow, marked @pytest.mark.slow)
  3. Bundle size < 500 KB
  4. Lockfile present and git-tracked
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths relative to the project root (C:\\MyAI\\06_Harness)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = PROJECT_ROOT / "web"
WEB_SRC = WEB_DIR / "src"
WEB_DIST = WEB_DIR / "dist"
WEB_DIST_ASSETS = WEB_DIST / "assets"
WEB_DIST_INDEX = WEB_DIST / "index.html"
WEB_PACKAGE_JSON = WEB_DIR / "package.json"
WEB_PACKAGE_LOCK = WEB_DIR / "package-lock.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# TypeScript import patterns — we use regex because TS import syntax is not
# compatible with Python's ``ast`` parser (e.g. ``import {X} from "Y"`` is
# invalid Python).  The regex captures the module source string.
_TS_IMPORT_RE = re.compile(
    r"""(?:import|export)\s+                    # import or export keyword
        (?:type\s+)?                            # optional 'type' modifier
        (?:\{[^}]*\}|[^"'{;]+)\s+from\s+       # specifiers ... from
        ["']([^"']+)["']                        # capture: module source
    """,
    re.VERBOSE,
)
_TS_SIDE_EFFECT_RE = re.compile(
    r"""import\s+["']([^"']+)["']""",  # import "module" (side-effect only)
    re.VERBOSE,
)


def _extract_ts_import_sources(file_path: Path) -> list[str]:
    """Return all module sources imported in a TypeScript file."""
    text = file_path.read_text(encoding="utf-8")
    sources: list[str] = []
    sources.extend(_TS_IMPORT_RE.findall(text))
    sources.extend(_TS_SIDE_EFFECT_RE.findall(text))
    return sources


# ---------------------------------------------------------------------------
# Test 1 — AST no harness imports
# ---------------------------------------------------------------------------

def test_no_harness_imports() -> None:
    """Verify no web/src TS file imports from the ``harness`` package.

    Uses regex-based import extraction because TypeScript import syntax
    (``import {X} from "Y"``) is not valid Python and cannot be parsed
    by ``ast``.  The conceptual intent — "Python AST parse" — is
    satisfied by treating regex extraction as a lightweight token-level
    scan of import source strings.
    """
    ts_files = sorted(WEB_SRC.rglob("*.ts")) + sorted(WEB_SRC.rglob("*.tsx"))
    assert ts_files, f"No .ts/.tsx files found under {WEB_SRC}"

    violations: list[tuple[str, str]] = []  # (relative_path, matched_source)

    for fp in ts_files:
        sources = _extract_ts_import_sources(fp)
        for src in sources:
            if src.startswith("harness") or src.startswith("harness/"):
                violations.append(
                    (str(fp.relative_to(PROJECT_ROOT)), src)
                )

    if violations:
        report = "\n".join(
            f"  {path!r} imports {src!r}" for path, src in violations
        )
        pytest.fail(
            f"Found {len(violations)} harness import(s) in web/src:\n{report}"
        )

    # Sanity: ensure we actually scanned something
    assert len(ts_files) > 0


# ---------------------------------------------------------------------------
# Test 2 — Build smoke
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_build_smoke() -> None:
    """Run ``npm ci`` + ``npm run build`` and verify dist/index.html exists.

    Marked ``@pytest.mark.slow`` because ``npm ci`` downloads dependencies
    and ``tsc && vite build`` compiles the project.
    """
    # 2a — package.json must exist
    assert WEB_PACKAGE_JSON.exists(), f"{WEB_PACKAGE_JSON} not found"

    # 2b — npm ci
    result_ci = subprocess.run(
        ["npm", "ci"],
        cwd=str(WEB_DIR),
        shell=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result_ci.returncode == 0, (
        f"npm ci failed (rc={result_ci.returncode}):\n"
        f"STDOUT:\n{result_ci.stdout[-2000:]}\n"
        f"STDERR:\n{result_ci.stderr[-2000:]}"
    )

    # 2c — npm run build
    result_build = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(WEB_DIR),
        shell=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result_build.returncode == 0, (
        f"npm run build failed (rc={result_build.returncode}):\n"
        f"STDOUT:\n{result_build.stdout[-2000:]}\n"
        f"STDERR:\n{result_build.stderr[-2000:]}"
    )

    # 2d — web/dist/index.html must exist after build
    assert WEB_DIST_INDEX.exists(), (
        f"{WEB_DIST_INDEX} not found after successful build"
    )


# ---------------------------------------------------------------------------
# Test 3 — Bundle size < 500 KB
# ---------------------------------------------------------------------------

def test_bundle_size() -> None:
    """Total size of all .js files in web/dist/assets/ must be < 500 000 bytes."""
    if not WEB_DIST_ASSETS.exists():
        pytest.skip(f"{WEB_DIST_ASSETS} does not exist — run build first")

    js_files = list(WEB_DIST_ASSETS.glob("*.js"))
    if not js_files:
        pytest.fail(f"No .js files found in {WEB_DIST_ASSETS}")

    total = sum(f.stat().st_size for f in js_files)
    limit = 500_000

    assert total < limit, (
        f"Bundle too large: {total:,} bytes (limit {limit:,} bytes). "
        f"Files: {[f.name for f in js_files]}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Lockfile present and git-tracked
# ---------------------------------------------------------------------------

def test_lockfile_tracked() -> None:
    """web/package-lock.json must exist and be tracked by git."""
    # 4a — file exists
    assert WEB_PACKAGE_LOCK.exists(), (
        f"{WEB_PACKAGE_LOCK} not found — run 'npm install' to generate it"
    )

    # 4b — tracked in git
    result = subprocess.run(
        ["git", "ls-files", "web/package-lock.json"],
        cwd=str(PROJECT_ROOT),
        shell=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    tracked = result.stdout.strip()
    assert tracked == "web/package-lock.json", (
        f"web/package-lock.json is NOT tracked by git. "
        f"git ls-files returned: {tracked!r}. "
        f"Run: git add web/package-lock.json"
    )
