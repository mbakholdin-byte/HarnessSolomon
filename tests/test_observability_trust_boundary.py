"""Phase 4.1: Trust boundary test — observability must not import agents/server/hooks.

Mirror of ``tests/test_hooks_trust_boundary.py`` pattern.

Uses AST parsing to detect any ``import`` of forbidden modules at the
top level of any ``harness/observability/*.py`` file. Forbidden:
    - ``harness.agents`` (production code)
    - ``harness.server`` (production code)
    - ``harness.hooks`` (Phase 4.0 framework, different package)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

OBSERVABILITY_DIR = Path(__file__).parent.parent / "harness" / "observability"
FORBIDDEN_MODULES = frozenset({"harness.agents", "harness.server", "harness.hooks"})


def _scan_file(path: Path) -> list[str]:
    """Return list of forbidden imports found in a single file."""
    if path.name in {"__pycache__"} or path.suffix != ".py":
        return []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0] + "." + alias.name.split(".")[1] \
                    if alias.name.startswith("harness.") and alias.name.count(".") >= 1 \
                    else alias.name
                if top in FORBIDDEN_MODULES:
                    violations.append(
                        f"{path.name}:{node.lineno}: imports {alias.name!r}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "harness":
                # Check if it goes into a forbidden subpackage.
                parts = node.module.split(".")
                if len(parts) >= 2 and f"{parts[0]}.{parts[1]}" in FORBIDDEN_MODULES:
                    violations.append(
                        f"{path.name}:{node.lineno}: from {node.module!r} import ..."
                    )
    return violations


def test_observability_does_not_import_agents() -> None:
    """No file under harness/observability/ may import harness.agents."""
    violations: list[str] = []
    for path in OBSERVABILITY_DIR.rglob("*.py"):
        violations.extend(_scan_file(path))
    assert not violations, (
        f"Trust boundary violation in harness/observability/:\n"
        + "\n".join(violations)
    )


def test_trust_boundary_files_exist() -> None:
    """Sanity: observability package exists."""
    assert OBSERVABILITY_DIR.is_dir()
    py_files = list(OBSERVABILITY_DIR.glob("*.py"))
    assert len(py_files) >= 5, f"Expected 5+ modules, found {len(py_files)}"


def test_no_runtime_import_violations() -> None:
    """At import time, no forbidden modules should be loaded DIRECTLY by observability.

    This checks the direct imports in our files (via AST), not the
    transitive closure of sys.modules (which would include transitive
    imports from harness.config, harness.server, etc. that are not
    initiated by observability). The AST scan in the first test is
    the source of truth.
    """
    # Verify each observability module only imports from allowed packages.
    allowed_prefixes = (
        "harness.observability",
        "harness.config",  # OK: needed for Settings type hints
    )
    forbidden_prefixes = FORBIDDEN_MODULES
    violations: list[str] = []
    for path in OBSERVABILITY_DIR.rglob("*.py"):
        if path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Import):
                target = node.names[0].name if node.names else None
            elif isinstance(node, ast.ImportFrom):
                target = node.module
            if target and target.startswith("harness."):
                # Is target an allowed prefix?
                if any(target == a or target.startswith(a + ".")
                       for a in allowed_prefixes):
                    continue
                # Is it a forbidden prefix?
                if any(target == f or target.startswith(f + ".")
                       for f in forbidden_prefixes):
                    violations.append(
                        f"{path.name}:{node.lineno}: imports {target!r}"
                    )
    assert not violations, (
        f"Trust boundary violation in harness/observability/:\n"
        + "\n".join(violations)
    )
