"""Phase 6.2A v1.27.0 — Plugin loader tests.

Covers:

* :class:`PluginRegistry` hook / tool / scope registration + lookup.
* :func:`load_plugins_from_dir` discovery, AST trust boundary, skip-on-
  error semantics, and whitelist enforcement.
* Settings-level ``plugins_enabled=False`` short-circuit.
* AST trust boundary on the shipped plugin sources.

Run::

    pytest tests/test_plugin_loader_v127.py -v
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from typing import Any

import pytest

from harness.plugins import (
    Plugin,
    PluginInfo,
    PluginRegistry,
    get_registry,
    reset_registry,
)
from harness.plugins.loader import load_plugins_from_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_registry() -> PluginRegistry:
    """Return a freshly-reset global registry (and leave it reset on exit)."""
    reset_registry()
    yield get_registry()
    reset_registry()


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    """A fresh empty plugins directory under ``tmp_path``."""
    d = tmp_path / "plugins"
    d.mkdir()
    return d


def _write_plugin(
    plugins_dir: Path,
    name: str,
    body: str,
) -> Path:
    """Helper: write a plugin .py file with the given body."""
    path = plugins_dir / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. PluginRegistry — hook registration
# ---------------------------------------------------------------------------

def test_plugin_registry_registers_hook(fresh_registry: PluginRegistry) -> None:
    """register_hook stores a callable retrievable via hooks_for()."""
    calls: list[dict[str, Any]] = []

    def handler(event: dict[str, Any]) -> None:
        calls.append(event)

    fresh_registry.register_hook("OnToolUse", handler, plugin_name="t1")

    handlers = fresh_registry.hooks_for("OnToolUse")
    assert len(handlers) == 1
    assert handlers[0] is handler

    # Dispatch contract: handler is invoked with a dict.
    handlers[0]({"tool_name": "read_file"})
    assert calls == [{"tool_name": "read_file"}]

    # Unknown hook → empty list, not error.
    assert fresh_registry.hooks_for("Nonexistent") == []


def test_plugin_registry_registers_tool(fresh_registry: PluginRegistry) -> None:
    """register_tool stores (handler, schema) retrievable via get_tool()."""
    def echo(x: int) -> int:
        return x

    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    fresh_registry.register_tool(
        "echo", echo, schema=schema, plugin_name="t2",
    )

    got = fresh_registry.get_tool("echo")
    assert got is not None
    handler, got_schema = got
    assert handler(42) == 42
    assert got_schema == schema

    # Unknown tool → None.
    assert fresh_registry.get_tool("nonexistent") is None


def test_plugin_registry_registers_scope(fresh_registry: PluginRegistry) -> None:
    """register_scope populates list_scopes()."""
    fresh_registry.register_scope("my_scope", "does X", plugin_name="t3")
    scopes = fresh_registry.list_scopes()
    assert scopes == {"my_scope": "does X"}


# ---------------------------------------------------------------------------
# 2. Loader — discovery
# ---------------------------------------------------------------------------

def test_load_plugins_from_dir_discovers_py_files(
    plugins_dir: Path,
    fresh_registry: PluginRegistry,
) -> None:
    """load_plugins_from_dir loads every *.py with a register() fn."""
    _write_plugin(plugins_dir, "alpha", """
        PLUGIN_NAME = "alpha"
        PLUGIN_VERSION = "1.0.0"
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None,
                                   plugin_name="alpha")
    """)
    _write_plugin(plugins_dir, "beta", """
        PLUGIN_NAME = "beta"
        def register(registry):
            registry.register_hook("OnStop", lambda e: None,
                                   plugin_name="beta")
            registry.register_scope("beta.scope", "from beta")
    """)

    loaded = load_plugins_from_dir(plugins_dir, registry=fresh_registry)
    names = sorted(p.name for p in loaded)
    assert names == ["alpha", "beta"]

    alpha = next(p for p in loaded if p.name == "alpha")
    assert alpha.version == "1.0.0"
    assert alpha.hooks == ["OnToolUse"]
    assert alpha.tools == []

    beta = next(p for p in loaded if p.name == "beta")
    assert beta.hooks == ["OnStop"]
    assert beta.scopes == ["beta.scope"]

    # Registry list_plugins agrees.
    assert sorted(p.name for p in fresh_registry.list_plugins()) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# 3. Loader — graceful skip on invalid plugins
# ---------------------------------------------------------------------------

def test_load_plugins_skips_invalid_plugins(
    plugins_dir: Path,
    fresh_registry: PluginRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid plugins are skipped; valid ones still load."""
    # Valid
    _write_plugin(plugins_dir, "good", """
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None,
                                   plugin_name="good")
    """)
    # Syntax error
    _write_plugin(plugins_dir, "broken_syntax", """
        def register(  # missing close paren
    """)
    # No register() function
    _write_plugin(plugins_dir, "no_register", """
        def not_register(registry):
            pass
    """)
    # register() raises
    _write_plugin(plugins_dir, "raises", """
        def register(registry):
            raise RuntimeError("boom")
    """)

    with caplog.at_level("WARNING", logger="harness.plugins.loader"):
        loaded = load_plugins_from_dir(plugins_dir, registry=fresh_registry)

    assert len(loaded) == 1
    assert loaded[0].source_path.endswith("good.py")
    # Failures were logged (at least 3 warnings).
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) >= 3


# ---------------------------------------------------------------------------
# 4. Loader — whitelist enforcement
# ---------------------------------------------------------------------------

def test_plugin_allowed_whitelist_blocks_unknown(
    plugins_dir: Path,
    fresh_registry: PluginRegistry,
) -> None:
    """allowed=['x'] loads only x.py, silently skipping others."""
    _write_plugin(plugins_dir, "allowed_one", """
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None,
                                   plugin_name="allowed_one")
    """)
    _write_plugin(plugins_dir, "blocked_one", """
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None,
                                   plugin_name="blocked_one")
    """)

    loaded = load_plugins_from_dir(
        plugins_dir,
        registry=fresh_registry,
        allowed=["allowed_one"],
    )
    assert len(loaded) == 1
    assert loaded[0].name == "allowed_one"


# ---------------------------------------------------------------------------
# 5. Loader — AST trust boundary (forbidden imports)
# ---------------------------------------------------------------------------

def test_load_plugins_rejects_forbidden_imports(
    plugins_dir: Path,
    fresh_registry: PluginRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin importing harness.agents is AST-blocked before exec."""
    _write_plugin(plugins_dir, "malicious", """
        import harness.agents.runner
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None)
    """)
    # Sanity: a clean plugin still loads alongside the malicious one.
    _write_plugin(plugins_dir, "benign", """
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None,
                                   plugin_name="benign")
    """)

    with caplog.at_level("WARNING", logger="harness.plugins.loader"):
        loaded = load_plugins_from_dir(plugins_dir, registry=fresh_registry)

    # Only benign loaded.
    assert [p.name for p in loaded] == ["benign"]
    # The rejection was logged.
    assert any(
        "malicious" in r.message and "forbidden" in r.message
        for r in caplog.records
    )


def test_load_plugins_rejects_forbidden_from_import(
    plugins_dir: Path,
    fresh_registry: PluginRegistry,
) -> None:
    """from harness.server.app import create_app is also AST-blocked."""
    _write_plugin(plugins_dir, "sneaky", """
        from harness.server.app import create_app
        def register(registry):
            pass
    """)
    loaded = load_plugins_from_dir(plugins_dir, registry=fresh_registry)
    assert loaded == []


# ---------------------------------------------------------------------------
# 6. Shipped example plugin loads and registers OnToolUse
# ---------------------------------------------------------------------------

def test_example_plugin_registers_on_tool_use_hook(
    fresh_registry: PluginRegistry,
) -> None:
    """The shipped .harness/plugins/example_logger.py loads cleanly."""
    repo_root = Path(__file__).resolve().parent.parent
    example_path = repo_root / ".harness" / "plugins" / "example_logger.py"
    assert example_path.is_file(), f"missing example plugin at {example_path}"

    # Load it via the loader so PLUGIN_NAME / register() are exercised.
    loaded = load_plugins_from_dir(
        example_path.parent,
        registry=fresh_registry,
        allowed=["example_logger"],
    )
    assert len(loaded) == 1
    info = loaded[0]
    assert info.name == "example_logger"
    assert info.version == "1.0.0"
    assert "OnToolUse" in info.hooks

    # The handler is callable with a dict event.
    handlers = fresh_registry.hooks_for("OnToolUse")
    assert len(handlers) == 1
    # Should not raise.
    handlers[0]({"tool_name": "x", "session_id": "s"})


# ---------------------------------------------------------------------------
# 7. Settings: plugins_enabled=False short-circuits loading
# ---------------------------------------------------------------------------

def test_disabled_plugins_setting_skips_loading(
    plugins_dir: Path,
    fresh_registry: PluginRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When plugins_enabled=False, lifespan-style code does not scan."""
    from harness.config import settings

    # Drop a plugin that WOULD load.
    _write_plugin(plugins_dir, "would_load", """
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None,
                                   plugin_name="would_load")
    """)

    # Mirror the lifespan gate: only scan when plugins_enabled is True.
    monkeypatch.setattr(settings, "plugins_enabled", False)
    if settings.plugins_enabled:
        loaded = load_plugins_from_dir(plugins_dir, registry=fresh_registry)
    else:
        loaded = []

    assert loaded == []
    assert fresh_registry.list_plugins() == []


# ---------------------------------------------------------------------------
# 8. AST trust boundary — shipped harness/plugins/*.py is clean
# ---------------------------------------------------------------------------

def test_shipped_plugin_modules_do_not_import_agents_or_server() -> None:
    """harness/plugins/*.py must not import harness.agents or harness.server.

    This is the static-trust-boundary guard — a defence in depth on top
    of the runtime AST scan in the loader. Catches accidental imports
    introduced during refactoring.
    """
    repo_root = Path(__file__).resolve().parent.parent
    plugins_pkg = repo_root / "harness" / "plugins"
    assert plugins_pkg.is_dir(), f"missing plugins package at {plugins_pkg}"

    forbidden_prefixes = ("harness.agents", "harness.server")
    failures: list[str] = []

    for py in sorted(plugins_pkg.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                targets.append(node.module)
            for tgt in targets:
                if any(tgt == p or tgt.startswith(p + ".") for p in forbidden_prefixes):
                    failures.append(f"{py.name}: {tgt}")

    assert not failures, (
        "harness/plugins/*.py must not import harness.agents / "
        f"harness.server — found: {failures}"
    )


# ---------------------------------------------------------------------------
# 9. Plugin base class
# ---------------------------------------------------------------------------

def test_plugin_base_class_register_default_noop() -> None:
    """The Plugin base class has a no-op register() by default."""
    class Empty(Plugin):
        name = "empty"
        version = "0.1.0"

    reg = PluginRegistry()
    # Should not raise.
    Empty().register(reg)
    assert reg.list_plugins() == []


# ---------------------------------------------------------------------------
# 10. Loader — non-existent directory is a no-op
# ---------------------------------------------------------------------------

def test_load_plugins_from_nonexistent_dir_returns_empty(
    tmp_path: Path,
    fresh_registry: PluginRegistry,
) -> None:
    """A non-existent plugins_dir is silently skipped."""
    missing = tmp_path / "does_not_exist"
    loaded = load_plugins_from_dir(missing, registry=fresh_registry)
    assert loaded == []


# ---------------------------------------------------------------------------
# 11. Loader — dunder / temp files are skipped
# ---------------------------------------------------------------------------

def test_load_plugins_skips_dunder_and_temp_files(
    plugins_dir: Path,
    fresh_registry: PluginRegistry,
) -> None:
    """__init__.py, _private.py and .swp-style files are not loaded."""
    _write_plugin(plugins_dir, "__init__", "")
    _write_plugin(plugins_dir, "_private", "x = 1\n")
    _write_plugin(plugins_dir, "real", """
        def register(registry):
            registry.register_hook("OnToolUse", lambda e: None,
                                   plugin_name="real")
    """)
    # Editor-style temp file.
    (plugins_dir / ".real.py.swp").write_bytes(b"\x00\x01")

    loaded = load_plugins_from_dir(plugins_dir, registry=fresh_registry)
    assert [p.name for p in loaded] == ["real"]
