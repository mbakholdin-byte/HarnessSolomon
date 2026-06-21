"""Phase 7.4 WI-04 v1.32.0 — Plugin install / uninstall CLI tests.

Covers:

* ``harness plugins install <name>`` — success, not-found, version-too-low,
  unsigned-warning.
* ``harness plugins uninstall <name>`` — success, not-loaded.

Run::

    pytest tests/test_install_cli.py -v --tb=short
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

import pytest

from harness.cli import (
    _cmd_plugins_install,
    _cmd_plugins_uninstall,
    _semver_gte,
)
from harness.plugins import get_registry, reset_registry
from harness.plugins.marketplace import MarketplaceManager
from harness.plugins.manifest_v2 import PluginManifestV2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_namespace(**kwargs: object) -> argparse.Namespace:
    """Build an argparse.Namespace with defaults for plugins commands."""
    defaults: dict[str, object] = {
        "plugin_name": "test-plugin",
        "marketplace_dir": None,
        "plugins_dir": None,
        "trust_registry": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _write_manifest_json(dir_path: Path, manifest: PluginManifestV2) -> Path:
    """Write a manifest as a ``<name>.json`` file in ``dir_path``."""
    p = dir_path / f"{manifest.name}.json"
    p.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


def _write_plugin_py(dir_path: Path, name: str, version: str = "1.0.0", body: str | None = None) -> Path:
    """Write a minimal ``<name>.py`` plugin source file."""
    if body is None:
        body = textwrap.dedent(f"""\
            PLUGIN_VERSION = {version!r}
            def register(registry):
                registry.register_hook(
                    "OnToolUse",
                    lambda event: None,
                    plugin_name={name!r},
                )
        """)
    p = dir_path / f"{name}.py"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def fresh_registry() -> None:
    """Reset the global PluginRegistry before and after each test."""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def tmp_marketplace(tmp_path: Path) -> Path:
    """Temporary marketplace directory with one valid manifest + source."""
    d = tmp_path / "marketplace"
    d.mkdir()
    return d


@pytest.fixture
def tmp_plugins(tmp_path: Path) -> Path:
    """Temporary plugins directory (empty)."""
    d = tmp_path / "plugins"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# semver_gte
# ---------------------------------------------------------------------------


def test_semver_gte() -> None:
    """Unit-test the semver comparison helper."""
    assert _semver_gte("1.31.0", "1.30.0") is True
    assert _semver_gte("1.31.0", "1.31.0") is True
    assert _semver_gte("1.31.0", "1.32.0") is False
    assert _semver_gte("2.0.0", "1.99.99") is True
    assert _semver_gte("0.9.0", "1.0.0") is False


# ---------------------------------------------------------------------------
# 1. test_install_plugin_success
# ---------------------------------------------------------------------------


def test_install_plugin_success(
    tmp_marketplace: Path,
    tmp_plugins: Path,
    fresh_registry: None,  # noqa: ARG001  (fixture side-effect)
) -> None:
    """Install a valid plugin from the marketplace → success."""
    name = "hello-world"
    manifest = PluginManifestV2(
        name=name,
        version="1.0.0",
        author="Test",
        description="A test plugin.",
        min_harness_version="1.30.0",  # <= current 1.31.0
        permissions=["tools.log"],
        entry_point=f"{name}.plugin",
        keywords=["test"],
    )
    manifest.validate()
    _write_manifest_json(tmp_marketplace, manifest)
    _write_plugin_py(tmp_marketplace, name, version="1.0.0")

    args = _make_namespace(
        plugin_name=name,
        marketplace_dir=str(tmp_marketplace),
        plugins_dir=str(tmp_plugins),
    )
    rc = _cmd_plugins_install(args)
    assert rc == 0

    # Plugin should now be loaded.
    registry = get_registry()
    info = registry.get_plugin(name)
    assert info is not None
    assert info.name == name
    assert info.version == "1.0.0"


# ---------------------------------------------------------------------------
# 2. test_install_plugin_not_found
# ---------------------------------------------------------------------------


def test_install_plugin_not_found(
    tmp_marketplace: Path,
    tmp_plugins: Path,
    fresh_registry: None,  # noqa: ARG001
) -> None:
    """Install a nonexistent plugin → error."""
    args = _make_namespace(
        plugin_name="nonexistent",
        marketplace_dir=str(tmp_marketplace),
        plugins_dir=str(tmp_plugins),
    )
    rc = _cmd_plugins_install(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# 3. test_install_plugin_version_too_low
# ---------------------------------------------------------------------------


def test_install_plugin_version_too_low(
    tmp_marketplace: Path,
    tmp_plugins: Path,
    fresh_registry: None,  # noqa: ARG001
) -> None:
    """Install with min_harness_version > current → error."""
    name = "futuristic"
    manifest = PluginManifestV2(
        name=name,
        version="1.0.0",
        author="Test",
        description="Requires a future version.",
        min_harness_version="99.0.0",  # way above current 1.31.0
        permissions=[],
        entry_point=f"{name}.plugin",
        keywords=[],
    )
    manifest.validate()
    _write_manifest_json(tmp_marketplace, manifest)
    _write_plugin_py(tmp_marketplace, name, version="1.0.0")

    args = _make_namespace(
        plugin_name=name,
        marketplace_dir=str(tmp_marketplace),
        plugins_dir=str(tmp_plugins),
    )
    rc = _cmd_plugins_install(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# 4. test_install_unsigned_plugin_warns
# ---------------------------------------------------------------------------


def test_install_unsigned_plugin_warns(
    tmp_marketplace: Path,
    tmp_plugins: Path,
    fresh_registry: None,  # noqa: ARG001
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unsigned plugin is installable but prints a warning."""
    name = "unsigned-one"
    manifest = PluginManifestV2(
        name=name,
        version="2.0.0",
        author="Test",
        description="Unsigned plugin.",
        min_harness_version="1.0.0",
        permissions=["tools.log"],
        entry_point=f"{name}.plugin",
        keywords=["test"],
        signature=None,  # explicitly unsigned
        public_key=None,
    )
    manifest.validate()
    _write_manifest_json(tmp_marketplace, manifest)
    _write_plugin_py(tmp_marketplace, name, version="2.0.0")

    args = _make_namespace(
        plugin_name=name,
        marketplace_dir=str(tmp_marketplace),
        plugins_dir=str(tmp_plugins),
    )
    rc = _cmd_plugins_install(args)
    assert rc == 0

    # Warning should appear on stderr.
    captured = capsys.readouterr()
    assert "unsigned" in captured.err.lower()
    assert "install at your own risk" in captured.err.lower()

    # Plugin should be loaded despite the warning.
    registry = get_registry()
    assert registry.get_plugin(name) is not None


# ---------------------------------------------------------------------------
# 5. test_uninstall_plugin_success
# ---------------------------------------------------------------------------


def test_uninstall_plugin_success(
    tmp_plugins: Path,
    fresh_registry: None,  # noqa: ARG001
) -> None:
    """Uninstall a loaded plugin → success."""
    name = "removable"

    # Manually load the plugin into the registry.
    registry = get_registry()
    from harness.plugins import PluginInfo
    registry.register_plugin(PluginInfo(
        name=name,
        version="1.0.0",
        source_path=str(tmp_plugins / f"{name}.py"),
        hooks=["OnToolUse"],
        tools=[],
        scopes=[],
    ))

    # Create the .py file in plugins dir.
    _write_plugin_py(tmp_plugins, name)

    args = _make_namespace(
        plugin_name=name,
        plugins_dir=str(tmp_plugins),
    )
    rc = _cmd_plugins_uninstall(args)
    assert rc == 0

    # Plugin should be gone from registry.
    assert registry.get_plugin(name) is None
    assert registry.is_disabled(name) is True

    # File should be removed.
    assert not (tmp_plugins / f"{name}.py").exists()


# ---------------------------------------------------------------------------
# 6. test_uninstall_plugin_not_loaded
# ---------------------------------------------------------------------------


def test_uninstall_plugin_not_loaded(
    tmp_plugins: Path,
    fresh_registry: None,  # noqa: ARG001
) -> None:
    """Uninstall a plugin that is not loaded → error."""
    args = _make_namespace(
        plugin_name="not-there",
        plugins_dir=str(tmp_plugins),
    )
    rc = _cmd_plugins_uninstall(args)
    assert rc == 1
