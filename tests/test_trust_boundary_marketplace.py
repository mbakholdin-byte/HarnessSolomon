"""WI-06 — Trust Boundary AST Tests (Phase 7.4 Marketplace).

Validates that Phase 7.4 files do NOT import forbidden modules:
  - ``manifest_v2.py``: no harness.agents, harness.server, harness.plugins.signature
  - ``marketplace.py`` (plugins): no harness.agents, harness.server
  - ``trust_registry.py``: no top-level harness.* imports (lazy import inside
    ``verify_signature`` is permitted)
  - ``marketplace.py`` (routes): no harness.agents

Uses ``ast.parse`` — same pattern as ``test_trust_boundary.py``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# ---------------------------------------------------------------------------
# Paths relative to project root (C:\\MyAI\\06_Harness)
# ---------------------------------------------------------------------------
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Rules: (file_relpath, forbidden_prefixes, description)
# ---------------------------------------------------------------------------
RULES = [
    (
        "harness/plugins/manifest_v2.py",
        ["harness.agents", "harness.server", "harness.plugins.signature"],
        "manifest_v2 no agents/server/signature",
    ),
    (
        "harness/plugins/marketplace.py",
        ["harness.agents", "harness.server"],
        "marketplace no agents/server",
    ),
    (
        "harness/security/trust_registry.py",
        ["harness.agents", "harness.server", "harness.plugins"],
        "trust_registry no harness imports (lazy ok)",
    ),
    (
        "harness/server/routes/marketplace.py",
        ["harness.agents"],
        "marketplace routes no agents",
    ),
]

# Files for test 6 — existing trust boundary files, regression guard.
_EXISTING_FILES: dict[str, list[str]] = {
    "harness/server/routes/hooks_admin.py": ["harness.agents"],
    "harness/server/routes/plugins_admin.py": ["harness.agents"],
    "harness/server/routes/observability_ws.py": ["harness.agents"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_imports(filepath: pathlib.Path) -> set[str]:
    """Parse Python file and return set of imported module names (all AST levels).

    Walks the entire AST, including imports inside function/class bodies.
    """
    src = filepath.read_text("utf-8")
    tree = ast.parse(src)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _extract_top_level_imports(filepath: pathlib.Path) -> set[str]:
    """Parse Python file and return set of top-level imported module names.

    Only checks ``ast.Module.body`` — excludes imports nested inside
    function/class bodies (e.g. lazy imports in method bodies).
    """
    src = filepath.read_text("utf-8")
    tree = ast.parse(src)
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _check_violations(
    imports: set[str],
    forbidden_prefixes: list[str],
) -> list[str]:
    """Return list of violating imports."""
    violations: list[str] = []
    for imp in imports:
        for prefix in forbidden_prefixes:
            if imp == prefix or imp.startswith(prefix + "."):
                violations.append(imp)
    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTrustBoundaryMarketplace:
    """Trust-boundary AST tests for Phase 7.4 files."""

    # ------------------------------------------------------------------
    # Test 1: manifest_v2.py — clean
    # ------------------------------------------------------------------

    def test_manifest_v2_no_forbidden_imports(self) -> None:
        """``manifest_v2.py`` does NOT import harness.agents, harness.server,
        or harness.plugins.signature."""
        path = PROJECT_ROOT / "harness" / "plugins" / "manifest_v2.py"
        assert path.is_file(), f"{path} not found"

        imports = _extract_imports(path)
        violations = _check_violations(
            imports,
            ["harness.agents", "harness.server", "harness.plugins.signature"],
        )
        assert not violations, (
            f"Trust boundary violation in manifest_v2.py: "
            f"forbidden imports {violations}"
        )

    # ------------------------------------------------------------------
    # Test 2: marketplace.py (plugins) — clean
    # ------------------------------------------------------------------

    def test_marketplace_no_forbidden_imports(self) -> None:
        """``marketplace.py`` does NOT import harness.agents or harness.server.

        May import ``harness.plugins.manifest_v2`` — allowed.
        """
        path = PROJECT_ROOT / "harness" / "plugins" / "marketplace.py"
        if not path.is_file():
            pytest.skip(f"marketplace.py not yet created at {path}")

        imports = _extract_imports(path)
        violations = _check_violations(
            imports,
            ["harness.agents", "harness.server"],
        )
        assert not violations, (
            f"Trust boundary violation in marketplace.py: "
            f"forbidden imports {violations}"
        )

    # ------------------------------------------------------------------
    # Test 3: trust_registry.py — top-level imports clean
    # ------------------------------------------------------------------

    def test_trust_registry_no_forbidden_imports(self) -> None:
        """``trust_registry.py`` has NO top-level imports from harness.agents,
        harness.server, or harness.plugins.

        Lazy import inside ``verify_signature`` method body is permitted.
        Only ``ast.Module.body`` (top-level) is checked.
        """
        path = PROJECT_ROOT / "harness" / "security" / "trust_registry.py"
        assert path.is_file(), f"{path} not found"

        # Use top-level-only extraction — lazy imports in method bodies
        # (harness.plugins.signature inside verify_signature) are allowed.
        imports = _extract_top_level_imports(path)
        violations = _check_violations(
            imports,
            ["harness.agents", "harness.server", "harness.plugins"],
        )
        assert not violations, (
            f"Trust boundary violation in trust_registry.py: "
            f"forbidden top-level imports {violations}"
        )

    # ------------------------------------------------------------------
    # Test 4: marketplace routes — clean
    # ------------------------------------------------------------------

    def test_marketplace_routes_no_forbidden_imports(self) -> None:
        """``marketplace.py`` routes do NOT import harness.agents.

        May import harness.server.auth.deps, harness.server.auth.scopes,
        harness.plugins.manifest_v2 — allowed.
        """
        path = PROJECT_ROOT / "harness" / "server" / "routes" / "marketplace.py"
        if not path.is_file():
            pytest.skip(f"marketplace.py routes not yet created at {path}")

        imports = _extract_imports(path)
        violations = _check_violations(imports, ["harness.agents"])
        assert not violations, (
            f"Trust boundary violation in marketplace routes: "
            f"forbidden imports {violations}"
        )

    # ------------------------------------------------------------------
    # Test 5: all Phase 7.4 files exist and parse
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "relpath",
        [
            "harness/plugins/manifest_v2.py",
            "harness/plugins/marketplace.py",
            "harness/security/trust_registry.py",
            "harness/server/routes/marketplace.py",
        ],
    )
    def test_all_phase74_files_exist(self, relpath: str) -> None:
        """Each Phase 7.4 file exists and is valid Python."""
        path = PROJECT_ROOT / relpath
        assert path.is_file(), f"{relpath} not found at {path}"
        source = path.read_text("utf-8")
        ast.parse(source, filename=str(path))  # Raises SyntaxError if invalid

    # ------------------------------------------------------------------
    # Test 6: regression — existing files unchanged
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "relpath, forbidden_prefixes",
        [
            ("harness/server/routes/hooks_admin.py", ["harness.agents"]),
            ("harness/server/routes/plugins_admin.py", ["harness.agents"]),
            ("harness/server/routes/observability_ws.py", ["harness.agents"]),
        ],
    )
    def test_no_new_forbidden_imports_in_existing_files(
        self, relpath: str, forbidden_prefixes: list[str]
    ) -> None:
        """Existing trust-boundary files have NOT acquired new forbidden imports."""
        path = PROJECT_ROOT / relpath
        assert path.is_file(), f"{relpath} not found at {path}"

        imports = _extract_imports(path)
        violations = _check_violations(imports, forbidden_prefixes)
        assert not violations, (
            f"Trust boundary violation in {relpath}: "
            f"forbidden imports {violations}"
        )
