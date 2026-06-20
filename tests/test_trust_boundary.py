"""WI-06 — Trust boundary AST tests for server route modules.

Validates that newly-created server route modules do NOT import
from ``harness.agents``. The trust model requires routes → agents
dependency to be one-way: agents call into routes via FastAPI
mounting, routes do NOT call back into agents.

Tests:
  1. ``hooks_admin.py`` — no ``harness.agents.*`` import
  2. ``plugins_admin.py`` — no ``harness.agents.*`` import
  3. ``observability_ws.py`` — no ``harness.agents.*`` import

Uses ``ast.parse`` — same pattern as ``test_hooks_trust_boundary.py``
and ``test_observability_trust_boundary.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths relative to the project root (C:\\MyAI\\06_Harness)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROUTES_DIR = PROJECT_ROOT / "harness" / "server" / "routes"

FORBIDDEN_PREFIXES: tuple[str, ...] = ("harness.agents",)

# Files to scan and their relative paths for readable assertions.
_SCAN_FILES: dict[str, Path] = {
    "hooks_admin.py": SERVER_ROUTES_DIR / "hooks_admin.py",
    "plugins_admin.py": SERVER_ROUTES_DIR / "plugins_admin.py",
    "observability_ws.py": SERVER_ROUTES_DIR / "observability_ws.py",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, module_name) for top-level absolute imports.

    Relative imports (``from .foo import ...``) are skipped — they
    cannot reach the forbidden ``harness.agents`` prefix without an
    explicit absolute ancestor.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — skip.
                continue
            if node.module:
                out.append((node.lineno, node.module))
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_hooks_admin_no_harness_agents_import() -> None:
    """``hooks_admin.py`` does NOT import ``harness.agents.*``."""
    path = _SCAN_FILES["hooks_admin.py"]
    assert path.is_file(), f"{path} not found"

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    violations: list[str] = []
    for lineno, module in _extract_imports(tree):
        for prefix in FORBIDDEN_PREFIXES:
            if module == prefix or module.startswith(prefix + "."):
                violations.append(
                    f"{path.name}:{lineno}: forbidden import {module!r}"
                )

    assert not violations, (
        f"Trust boundary violation in hooks_admin.py:\n  "
        + "\n  ".join(violations)
    )


def test_plugins_admin_no_harness_agents_import() -> None:
    """``plugins_admin.py`` does NOT import ``harness.agents.*``."""
    path = _SCAN_FILES["plugins_admin.py"]
    assert path.is_file(), f"{path} not found"

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    violations: list[str] = []
    for lineno, module in _extract_imports(tree):
        for prefix in FORBIDDEN_PREFIXES:
            if module == prefix or module.startswith(prefix + "."):
                violations.append(
                    f"{path.name}:{lineno}: forbidden import {module!r}"
                )

    assert not violations, (
        f"Trust boundary violation in plugins_admin.py:\n  "
        + "\n  ".join(violations)
    )


def test_observability_ws_no_harness_agents_import() -> None:
    """``observability_ws.py`` does NOT import ``harness.agents.*``."""
    path = _SCAN_FILES["observability_ws.py"]
    assert path.is_file(), f"{path} not found"

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    violations: list[str] = []
    for lineno, module in _extract_imports(tree):
        for prefix in FORBIDDEN_PREFIXES:
            if module == prefix or module.startswith(prefix + "."):
                violations.append(
                    f"{path.name}:{lineno}: forbidden import {module!r}"
                )

    assert not violations, (
        f"Trust boundary violation in observability_ws.py:\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# Sanity: files exist and parse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label",
    ["hooks_admin.py", "plugins_admin.py", "observability_ws.py"],
)
def test_file_exists_and_parses(label: str) -> None:
    """Each target file exists and is valid Python."""
    path = _SCAN_FILES[label]
    assert path.is_file(), f"{label} not found at {path}"
    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path))  # Raises SyntaxError if invalid.
