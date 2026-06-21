"""Phase 7.4 WI-05: Trust Registry tests.

Tests for ``harness.security.trust_registry.TrustRegistry`` — load,
validate, add/remove, hot-reload, and config env-var override.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.security.trust_registry import (
    TrustRegistry,
    TrustRegistryValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_valid_registry(path: Path, keys: list[dict] | None = None) -> None:
    """Write a valid trust-registry.json to ``path``."""
    if keys is None:
        keys = [
            {
                "name": "official-harness-team",
                "public_key": "ed25519:abc123def456",
                "added_at": "2026-06-23T00:00:00Z",
                "notes": "Official Marketplace releases",
            },
        ]
    data = {
        "version": "1",
        "public_keys": keys,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: load valid JSON
# ---------------------------------------------------------------------------


def test_trust_registry_load_valid(tmp_path: Path) -> None:
    """Load a valid trust-registry.json and verify keys are in memory."""
    reg_path = tmp_path / "trust-registry.json"
    _write_valid_registry(reg_path)

    registry = TrustRegistry(path=reg_path)
    registry.load()

    assert registry.get_key("official-harness-team") == "ed25519:abc123def456"
    keys = registry.list_keys()
    assert len(keys) == 1
    assert keys[0]["name"] == "official-harness-team"
    assert keys[0]["public_key"] == "ed25519:abc123def456"


# ---------------------------------------------------------------------------
# Test 2: invalid JSON / schema → raises
# ---------------------------------------------------------------------------


def test_trust_registry_invalid_json_rejected(tmp_path: Path) -> None:
    """Various invalid JSON/schema inputs must raise appropriate errors."""
    reg_path = tmp_path / "trust-registry.json"

    # Case A: not JSON
    reg_path.write_text("not json {{{", encoding="utf-8")
    registry = TrustRegistry(path=reg_path)
    with pytest.raises(json.JSONDecodeError):
        registry.load()

    # Case B: wrong version
    reg_path.write_text(
        json.dumps({"version": "2", "public_keys": []}),
        encoding="utf-8",
    )
    with pytest.raises(TrustRegistryValidationError, match="version"):
        registry.load()

    # Case C: empty public_keys list
    reg_path.write_text(
        json.dumps({"version": "1", "public_keys": []}),
        encoding="utf-8",
    )
    with pytest.raises(TrustRegistryValidationError, match="non-empty"):
        registry.load()

    # Case D: missing 'name' in entry
    reg_path.write_text(
        json.dumps({
            "version": "1",
            "public_keys": [{"public_key": "ed25519:abc"}],
        }),
        encoding="utf-8",
    )
    with pytest.raises(TrustRegistryValidationError, match="missing required"):
        registry.load()

    # Case E: public_key doesn't start with 'ed25519:'
    reg_path.write_text(
        json.dumps({
            "version": "1",
            "public_keys": [{"name": "bad", "public_key": "rsa:abc"}],
        }),
        encoding="utf-8",
    )
    with pytest.raises(TrustRegistryValidationError, match="must start with"):
        registry.load()

    # Case F: missing version key entirely
    reg_path.write_text(
        json.dumps({"public_keys": [{"name": "x", "public_key": "ed25519:abc"}]}),
        encoding="utf-8",
    )
    with pytest.raises(TrustRegistryValidationError, match="Missing required"):
        registry.load()

    # Case G: duplicate name
    reg_path.write_text(
        json.dumps({
            "version": "1",
            "public_keys": [
                {"name": "dup", "public_key": "ed25519:aaa"},
                {"name": "dup", "public_key": "ed25519:bbb"},
            ],
        }),
        encoding="utf-8",
    )
    with pytest.raises(TrustRegistryValidationError, match="duplicate"):
        registry.load()


# ---------------------------------------------------------------------------
# Test 3: add + get + remove
# ---------------------------------------------------------------------------


def test_trust_registry_add_remove_key(tmp_path: Path) -> None:
    """Add, get, list, and remove keys."""
    reg_path = tmp_path / "trust-registry.json"
    registry = TrustRegistry(path=reg_path)

    # Start empty
    assert registry.list_keys() == []

    # Add a key
    registry.add_key("team-alpha", "ed25519:aaa111")
    assert registry.get_key("team-alpha") == "ed25519:aaa111"
    assert len(registry.list_keys()) == 1

    # Add another
    registry.add_key("team-beta", "ed25519:bbb222")
    assert len(registry.list_keys()) == 2

    # Get non-existent
    assert registry.get_key("no-such") is None

    # Remove existing
    assert registry.remove_key("team-alpha") is True
    assert registry.get_key("team-alpha") is None
    assert len(registry.list_keys()) == 1

    # Remove non-existent
    assert registry.remove_key("no-such") is False

    # Verify file was persisted
    assert reg_path.exists()
    raw = json.loads(reg_path.read_text(encoding="utf-8"))
    assert raw["version"] == "1"
    assert len(raw["public_keys"]) == 1
    assert raw["public_keys"][0]["name"] == "team-beta"

    # add_key with bad public_key prefix
    with pytest.raises(ValueError, match="must start with"):
        registry.add_key("bad", "not-ed25519")


# ---------------------------------------------------------------------------
# Test 4: hot-reload
# ---------------------------------------------------------------------------


def test_trust_registry_hot_reload(tmp_path: Path) -> None:
    """Write to file externally, check_hot_reload returns True and loads new keys."""
    reg_path = tmp_path / "trust-registry.json"
    _write_valid_registry(reg_path)

    registry = TrustRegistry(path=reg_path)
    registry.load()

    # No change yet
    assert registry.check_hot_reload() is False

    # Write new content externally (simulate external edit)
    _write_valid_registry(
        reg_path,
        keys=[
            {"name": "new-team", "public_key": "ed25519:new999"},
        ],
    )

    # Hot-reload should detect and load
    assert registry.check_hot_reload() is True
    assert registry.get_key("new-team") == "ed25519:new999"
    assert registry.get_key("official-harness-team") is None  # old key gone

    # Second check: no change
    assert registry.check_hot_reload() is False


# ---------------------------------------------------------------------------
# Test 5: env var override
# ---------------------------------------------------------------------------


def test_trust_registry_env_var_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """HARNESS_TRUST_REGISTRY_PATH overrides the config setting."""
    custom_path = tmp_path / "custom-trust.json"
    monkeypatch.setenv("HARNESS_TRUST_REGISTRY_PATH", str(custom_path))

    from harness.config import Settings
    s = Settings()
    assert s.trust_registry_path == custom_path

    # Also check default poll_interval is set
    assert s.trust_registry_poll_interval == 5
    assert s.trust_registry_hot_reload is True
