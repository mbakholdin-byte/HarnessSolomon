"""Phase 7.4 WI-02 v1.32.0 — Manifest v2 tests.

Covers:
* Valid v2 manifest construction, validation, and serialization.
* Backward compatibility with v1 manifests.
* Rejection of invalid semver and permission strings.
* Loader integration: MANIFEST_V2 priority + v1 fallback.
* Warning on unsigned manifests.

Run::

    pytest tests/test_manifest_v2.py -v --tb=short
"""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from harness.plugins import PluginManifestV2, get_registry, reset_registry
from harness.plugins.loader import load_plugins_from_dir

logger = logging.getLogger("harness.plugins.manifest_v2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_dict() -> dict:
    """Return the minimum valid v2 manifest dict (5 required fields)."""
    return {
        "name": "test-plugin",
        "version": "1.0.0",
        "author": "Test Author",
        "description": "A test plugin",
        "entry_point": "test_pkg.plugin",
    }


def _write_plugin(path: Path, name: str, body: str) -> Path:
    """Write a .py plugin file under ``path``."""
    p = path / f"{name}.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Valid v2 manifest
# ---------------------------------------------------------------------------


def test_manifest_v2_valid() -> None:
    """A minimal v2 manifest with 5 mandatory fields validates cleanly."""
    m = PluginManifestV2.from_dict(_make_minimal_dict())
    m.validate()  # should not raise

    assert m.name == "test-plugin"
    assert m.version == "1.0.0"
    assert m.author == "Test Author"
    assert m.description == "A test plugin"
    assert m.entry_point == "test_pkg.plugin"
    # v2 fields get sentinel defaults when loaded from v1 dict.
    assert m.min_harness_version == ""
    assert m.permissions == []
    assert m.signature is None
    assert m.public_key is None
    # Optional fields default correctly.
    assert m.homepage is None
    assert m.repository is None
    assert m.keywords == []


# ---------------------------------------------------------------------------
# 2. Full v2 manifest with signature
# ---------------------------------------------------------------------------


def test_manifest_v2_with_signature() -> None:
    """A full v2 manifest with signature + public_key validates cleanly."""
    data = {
        "name": "signed-plugin",
        "version": "2.0.0-beta.1",
        "author": "Signer",
        "description": "Signed plugin",
        "min_harness_version": "1.32.0",
        "permissions": ["read_files", "write_config"],
        "signature": "a" * 128,   # hex-encoded Ed25519 sig (128 hex chars)
        "public_key": "b" * 64,   # hex-encoded Ed25519 pub key (64 hex chars)
        "entry_point": "signed_pkg.plugin",
        "homepage": "https://example.com",
        "repository": "https://github.com/example/plugin",
        "keywords": ["test", "signed"],
    }
    m = PluginManifestV2.from_dict(data)
    m.validate()

    assert m.name == "signed-plugin"
    assert m.version == "2.0.0-beta.1"
    assert m.min_harness_version == "1.32.0"
    assert m.permissions == ["read_files", "write_config"]
    assert m.signature == "a" * 128
    assert m.public_key == "b" * 64
    assert m.homepage == "https://example.com"
    assert m.keywords == ["test", "signed"]
    # Not backward-compat — has v2 fields.
    assert m.is_backward_compat_v1() is False

    # to_dict / from_dict round-trip.
    d = m.to_dict()
    m2 = PluginManifestV2.from_dict(d)
    assert m2.to_dict() == d


# ---------------------------------------------------------------------------
# 3. v1 backward compatibility
# ---------------------------------------------------------------------------


def test_manifest_v1_still_loads() -> None:
    """A v1 manifest (no min_harness_version, permissions, signature) loads."""
    data = {
        "name": "v1-plugin",
        "version": "0.5.0",
        "author": "v1 Author",
        "description": "Legacy plugin",
        "entry_point": "v1_pkg.plugin",
        "homepage": "https://v1.example.com",
        "repository": "https://github.com/v1/plugin",
        "keywords": ["legacy", "v1"],
    }
    m = PluginManifestV2.from_dict(data)
    m.validate()  # should not raise — v2 fields are at defaults

    assert m.name == "v1-plugin"
    assert m.version == "0.5.0"
    assert m.min_harness_version == ""    # sentinel
    assert m.permissions == []             # sentinel
    assert m.signature is None
    assert m.public_key is None
    assert m.keywords == ["legacy", "v1"]

    # Marked as backward-compatible.
    assert m.is_backward_compat_v1() is True


# ---------------------------------------------------------------------------
# 4. Invalid semver → rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_version,field",
    [
        ("not-a-version", "version"),
        ("1.0", "version"),            # only major.minor
        ("01.1.0", "version"),         # leading zero
        ("abc.def.ghi", "version"),
        ("1.2.3.4", "version"),        # too many segments
        ("1.2.3 ", "version"),         # trailing space
    ],
)
def test_manifest_v2_invalid_semver_version(
    bad_version: str, field: str,
) -> None:
    """Invalid semver in the version field raises ValueError."""
    data = _make_minimal_dict()
    data["version"] = bad_version
    m = PluginManifestV2.from_dict(data)
    with pytest.raises(ValueError, match="Invalid semver"):
        m.validate()


def test_manifest_v2_invalid_semver_min_harness() -> None:
    """Invalid semver in min_harness_version raises ValueError."""
    data = _make_minimal_dict()
    data["min_harness_version"] = "not-a-version"
    m = PluginManifestV2.from_dict(data)
    with pytest.raises(ValueError, match="Invalid semver"):
        m.validate()


# ---------------------------------------------------------------------------
# 5. Invalid permissions → rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_perms",
    [
        ["Invalid_Caps"],        # uppercase
        ["read files"],          # space
        [""],                    # empty string
        ["read\nfiles"],         # newline
        ["read.files!"],         # special char
    ],
)
def test_manifest_v2_invalid_permissions(bad_perms: list[str]) -> None:
    """Invalid permission strings raise ValueError."""
    data = _make_minimal_dict()
    data["permissions"] = bad_perms
    m = PluginManifestV2.from_dict(data)
    with pytest.raises(ValueError, match="Invalid permission"):
        m.validate()


# ---------------------------------------------------------------------------
# 6. Loader: MANIFEST_V2 priority + v1 fallback
# ---------------------------------------------------------------------------


def test_manifest_v2_loader_fallback(tmp_path: Path) -> None:
    """Loader picks MANIFEST_V2 dict over PLUGIN_NAME when both present.

    Also tests that a plugin without MANIFEST_V2 still loads via v1
    PLUGIN_NAME/PLUGIN_VERSION fallback.
    """
    reset_registry()
    registry = get_registry()

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    # Plugin with MANIFEST_V2 only.
    _write_plugin(plugins_dir, "v2_plugin", """
        MANIFEST_V2 = {
            "name": "v2-plugin",
            "version": "2.1.0",
            "author": "v2",
            "description": "A v2 plugin",
            "entry_point": "v2.mod",
        }
        def register(registry):
            registry.register_hook("OnStart", lambda e: None,
                                   plugin_name="v2-plugin")
    """)

    # Plugin without MANIFEST_V2 — uses v1 PLUGIN_NAME/PLUGIN_VERSION.
    _write_plugin(plugins_dir, "v1_plugin", """
        PLUGIN_NAME = "v1-plugin"
        PLUGIN_VERSION = "0.9.0"
        def register(registry):
            registry.register_hook("OnStart", lambda e: None,
                                   plugin_name="v1-plugin")
    """)

    loaded = load_plugins_from_dir(plugins_dir, registry=registry)

    # Both plugins loaded.
    names = sorted(p.name for p in loaded)
    assert names == ["v1-plugin", "v2-plugin"]

    v2_info = next(p for p in loaded if p.name == "v2-plugin")
    v1_info = next(p for p in loaded if p.name == "v1-plugin")
    assert v2_info.version == "2.1.0"
    assert v1_info.version == "0.9.0"

    reset_registry()


def test_manifest_v2_loader_rejects_invalid_manifest(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin with an invalid MANIFEST_V2 dict is skipped by the loader."""
    reset_registry()
    registry = get_registry()

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    # MANIFEST_V2 with bad semver.
    _write_plugin(plugins_dir, "bad_manifest", """
        MANIFEST_V2 = {
            "name": "bad",
            "version": "not-a-version",
            "author": "x",
            "description": "x",
            "entry_point": "bad.mod",
        }
        def register(registry):
            pass
    """)

    with caplog.at_level("WARNING", logger="harness.plugins.loader"):
        loaded = load_plugins_from_dir(plugins_dir, registry=registry)

    assert loaded == []
    # The skip was logged.
    assert any(
        "bad_manifest" in r.message and "skipping" in r.message
        for r in caplog.records
    )

    reset_registry()


# ---------------------------------------------------------------------------
# 7. No-signature warning
# ---------------------------------------------------------------------------


def test_manifest_v2_no_signature_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A manifest without signature is valid but logs a warning."""
    data = _make_minimal_dict()
    data["min_harness_version"] = "1.32.0"
    data["permissions"] = ["read_files"]

    m = PluginManifestV2.from_dict(data)

    with caplog.at_level("WARNING", logger="harness.plugins.manifest_v2"):
        m.validate()

    # Validation passes (no exception).
    assert m.signature is None

    # Warning was emitted.
    assert any(
        "no signature" in r.message.lower()
        for r in caplog.records
    ), f"Expected 'no signature' warning in: {[r.message for r in caplog.records]}"
