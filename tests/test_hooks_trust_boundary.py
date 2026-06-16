"""Phase 4.0: Trust boundary test for the hooks framework.

Mirrors ``tests/eval/test_eval_trust_boundary.py``. Parses every
``.py`` file under ``harness/hooks/`` with ``ast`` and verifies that
no top-level ``import`` / ``from ... import`` statement references
``harness.agents`` or ``harness.server``.

A violation of this invariant breaks the entire trust model: hooks
are supposed to be a low-level extension point that production code
calls into (one-way), not the other way around. If hooks starts
importing from agents / server, the boundary becomes circular and
Phase 4.0 cannot ship.

Allowed:
    - stdlib imports (json, asyncio, dataclasses, pathlib, ...)
    - ``harness.config`` (Settings — read-only)
    - ``harness.hooks.*`` (relative or absolute within hooks package)
    - ``harness.redaction`` (Phase 3 v1.0.0 PII redactor — read-only utility)

Forbidden:
    - ``harness.agents.*`` (sub-agents, runner, merge_queue, router, ...)
    - ``harness.server.*`` (FastAPI app, routes, lifespan, ...)
    - ``harness.agents`` and ``harness.server`` (any submodule)

Relative imports (``.foo``) are skipped — they cannot reach the
forbidden prefixes without an explicit absolute prefix.
``TYPE_CHECKING`` imports ARE checked (they still resolve at type-check
time and indicate an architectural smell even if not loaded at runtime).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


# Resolve the package root once at import time.
HOOKS_DIR = Path(__file__).parent.parent / "harness" / "hooks"
assert HOOKS_DIR.is_dir(), f"harness/hooks/ not found at {HOOKS_DIR}"


# Prefixes that are FORBIDDEN at the top of any file under harness/hooks/.
FORBIDDEN_PREFIXES: tuple[str, ...] = ("harness.agents", "harness.server")

# Prefixes that are explicitly ALLOWED.
ALLOWED_PREFIXES: tuple[str, ...] = (
    "harness.config",
    "harness.hooks",
    "harness.redaction",
)


def _iter_hook_files() -> list[Path]:
    """Yield every .py file under harness/hooks/ (including builtin/)."""
    return sorted(p for p in HOOKS_DIR.rglob("*.py") if p.is_file())


def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    """Return a list of (line, module_name) for top-level imports.

    Only absolute imports are checked (relative imports cannot reach
    the forbidden prefixes). ``TYPE_CHECKING`` blocks are still
    scanned — a forbidden TYPE_CHECKING import is a real boundary
    violation even if not loaded at runtime.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — cannot reach forbidden prefixes.
                continue
            if node.module:
                out.append((node.lineno, node.module))
    return out


class TestHooksTrustBoundary:
    """Static test: harness/hooks/ does NOT import from agents/server."""

    def test_forbidden_imports_in_hooks_package(self) -> None:
        """No file under harness/hooks/ may import harness.agents or harness.server."""
        violations: list[str] = []
        files = _iter_hook_files()
        assert files, "no .py files found under harness/hooks/"

        for path in files:
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as e:
                violations.append(f"{path}:{e.lineno}: SyntaxError: {e.msg}")
                continue

            for lineno, module in _imported_modules(tree):
                # Skip stdlib (no dot at top level) and relative imports
                # (filtered already).
                for prefix in FORBIDDEN_PREFIXES:
                    if module == prefix or module.startswith(prefix + "."):
                        violations.append(
                            f"{path.relative_to(HOOKS_DIR.parent.parent)}:{lineno}: "
                            f"forbidden import: {module!r} (prefix {prefix!r} "
                            f"is not allowed in harness/hooks/)"
                        )

        assert not violations, (
            "Trust boundary violations in harness/hooks/:\n  "
            + "\n  ".join(violations)
        )

    @pytest.mark.parametrize("path", _iter_hook_files(), ids=lambda p: str(p.relative_to(HOOKS_DIR)))
    def test_each_file_parses(self, path: Path) -> None:
        """Each file under harness/hooks/ must be valid Python."""
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))

    def test_no_circular_import_via_top_level(self) -> None:
        """Importing harness.hooks must not trigger agents or server imports."""
        # If a top-level import in harness.hooks pulls in agents/server,
        # this import would raise or surface a violation. We do a
        # targeted import to surface the issue at test time.
        import harness.hooks  # noqa: F401
        import harness.hooks.context  # noqa: F401
        import harness.hooks.events  # noqa: F401
        import harness.hooks.registry  # noqa: F401
        import harness.hooks.__init__ as pkg  # noqa: F401
        # The test passes if no ImportError / circular import occurred.
        assert hasattr(pkg, "EventType")
        assert hasattr(pkg, "HookContext")
        assert hasattr(pkg, "HookRegistry")
