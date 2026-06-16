"""Phase 3 B-mini: trust boundary test for harness/eval/.

Mirror of ``tests/test_runner_does_not_import_v150.py``. The
``harness/eval/`` package must NOT import from ``harness.agents/`` or
``harness.server/`` (production code).

This test uses ``ast`` to extract ONLY real import statements, then
checks those for forbidden prefixes. This avoids the false-positive
problems of regex-on-source (docstrings, comments, Sphinx refs that
mention forbidden module names in prose).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Forbidden import prefixes. These point to packages that production
# code depends on; harness/eval/ must not.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "harness.agents",
    "harness.server",
)


def _extract_imports(source: str) -> list[str]:
    """Parse ``source`` and return the dotted module path of every import.

    Handles both ``import x.y.z`` and ``from x.y.z import a, b``. Skips
    relative imports (those starting with ``.``) since they cannot
    reach ``harness.agents`` or ``harness.server`` from inside
    ``harness.eval`` without an explicit absolute prefix.
    """
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — count the dots, prepend the package
                # path. We don't know the package's dotted path from
                # AST alone, so just skip these (they cannot reach
                # harness.agents/ or harness.server/ from harness/eval/
                # without ``from .agents import ...`` which would still
                # resolve to ``harness.eval.agents``).
                continue
            if node.module:
                imports.append(node.module)
    return imports


@pytest.mark.parametrize(
    "source_file",
    sorted(Path("harness/eval").glob("**/*.py")),
    ids=lambda p: str(p).replace("\\", "/"),
)
def test_eval_does_not_import_forbidden(source_file: Path) -> None:
    """harness/eval/ must NOT import from harness.agents or harness.server."""
    source = source_file.read_text(encoding="utf-8")
    imports = _extract_imports(source)
    for imported in imports:
        for forbidden in FORBIDDEN_PREFIXES:
            assert not imported.startswith(forbidden + ".") and imported != forbidden, (
                f"{source_file} imports forbidden module '{imported}' "
                f"(matches forbidden prefix '{forbidden}'). "
                f"harness/eval/ is a read-only utility layer; "
                f"move code to harness/agents/ or harness/server/ if it "
                f"needs production access."
            )
