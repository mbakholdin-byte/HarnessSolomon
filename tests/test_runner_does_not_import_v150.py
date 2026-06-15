"""Phase 3 v1.5.0 Step 6: trust-boundary test (parametrized).

Verifies that ``harness/agents/runner.py`` does NOT directly import
any of the v1.5.0 production modules:

  - ``PrivacyZoneFilter`` (harness.privacy.zone_filter)
  - ``PreCompactHook``   (harness.agents.pre_compact)
  - ``TimeBasedCompactionTrigger`` (harness.agents.idle_trigger)

Mirrors the v1.3.1 ``test_runner_does_not_import_tool_offloader`` and
v1.4.0 ``test_runner_does_not_import_{reflection_loop,session_lifecycle,
compact_trigger}`` patterns. The runner accesses these via:

  * factory callable kwargs (``privacy_zones``, ``pre_compact_hook``,
    ``idle_trigger`` are passed to ``ToolRuntime`` / ``ContextCompactor``
    from the lifespan, not constructed inside the runner);
  * duck-typed ``Any`` attributes (``self._privacy_zones``,
    ``self._idle_trigger``).

A static check on the runner's source file ensures the trust boundary
is preserved even as new features are added.

See: docs/PHASE3-privacy-precompact-time.md (Trust boundary section)
and Phase 3 v1.5.0 plan (Plan agent CONCERN C6 → 1 parametrized test).
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Symbols that MUST NOT appear in ``harness.agents.runner`` source code
# as direct imports. The runner is allowed to use these symbols via
# DI (factory closures, constructor kwargs) but never via
# ``from harness.privacy import PrivacyZoneFilter`` or similar.
V150_FORBIDDEN_IMPORTS: list[tuple[str, str, str]] = [
    # (symbol, module_path, comment)
    (
        "PrivacyZoneFilter",
        "harness.privacy.zone_filter",
        "Tier 1 privacy filter — wired via lifespan, not runner",
    ),
    (
        "PreCompactHook",
        "harness.agents.pre_compact",
        "Pre-compact state save — wired via lifespan, not runner",
    ),
    (
        "TimeBasedCompactionTrigger",
        "harness.agents.idle_trigger",
        "Time/turn/hybrid compaction trigger — wired via lifespan, not runner",
    ),
]


@pytest.mark.parametrize(
    ("symbol", "module_path", "_comment"),
    V150_FORBIDDEN_IMPORTS,
    ids=[t[0] for t in V150_FORBIDDEN_IMPORTS],
)
def test_runner_does_not_import_v150_module(
    symbol: str, module_path: str, _comment: str,
) -> None:
    """runner.py must not import v1.5.0 new modules (trust boundary).

    Static check on the runner's source file. Skips comments, docstrings,
    and Sphinx cross-references (``:class:``/``:func:``/``:meth:``)
    to avoid false positives on documentation.
    """
    runner_path = Path("harness/agents/runner.py")
    src = runner_path.read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        # Skip blank lines, comments, docstrings, and Sphinx refs.
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        if ":class:" in line or ":func:" in line or ":meth:" in line:
            continue
        # Direct import patterns to forbid.
        forbidden_patterns = (
            f"from {module_path} import",
            f"from {module_path} import {symbol}",
            f"import {module_path}",
            f"import {module_path}.{symbol}",
        )
        for pattern in forbidden_patterns:
            if pattern in line:
                pytest.fail(
                    f"runner.py has a real import of v1.5.0 module "
                    f"{module_path!r} (symbol={symbol!r}): {line!r}"
                )


def test_runner_does_not_construct_v150_objects() -> None:
    """runner.py must not construct any of the v1.5.0 classes.

    This complements ``test_runner_does_not_import_v150_module``
    by catching inline constructions like ``PrivacyZoneFilter(rules=...)``
    in the runner (which would bypass the factory pattern).

    Allowed forms in runner.py:
      * ``getattr(self, "_privacy_zones", None)`` (duck-typed access)
      * Constructor kwargs ``privacy_zones=...`` (DI)
      * Pass-through to ``ToolRuntime`` / ``ContextCompactor``

    Forbidden forms:
      * ``PrivacyZoneFilter(`` anywhere
      * ``PreCompactHook(`` anywhere
      * ``TimeBasedCompactionTrigger(`` anywhere
    """
    runner_path = Path("harness/agents/runner.py")
    src = runner_path.read_text(encoding="utf-8")
    forbidden_ctors = (
        "PrivacyZoneFilter(",
        "PreCompactHook(",
        "TimeBasedCompactionTrigger(",
    )
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":class:" in line or ":func:" in line or ":meth:" in line:
            continue
        for ctor in forbidden_ctors:
            if ctor in line:
                pytest.fail(
                    f"runner.py constructs v1.5.0 class {ctor!r} "
                    f"inline (should be DI via factory): {line!r}"
                )
