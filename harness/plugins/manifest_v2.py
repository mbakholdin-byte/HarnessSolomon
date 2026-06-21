"""Phase 7.4 WI-02 v1.32.0 — Plugin Manifest v2 + Backward Compat.

Defines :class:`PluginManifestV2` — a dataclass-based plugin manifest with
semver validation, permission format checking, serialization, and backward-
compatibility detection with v1 manifests.

Trust boundary (CRITICAL):
    This module imports ONLY stdlib. It does NOT import ``harness.agents``,
    ``harness.server``, or ``harness.plugins.signature``. The AST test in
    ``tests/test_plugin_loader_v127.py`` enforces this at collection time.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PluginManifestV2"]

log = logging.getLogger("harness.plugins.manifest_v2")

# ── Semver regex ─────────────────────────────────────────────────────
# Covers: MAJOR.MINOR.PATCH with optional pre-release (-alpha.1) and
# build metadata (+build.2024). Leading zeros are NOT allowed in numeric
# identifiers (per semver 2.0.0 §2).
_SEMVER_RE = re.compile(
    r"^"
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"           # MAJOR.MINOR.PATCH
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"                  # pre-release
    r"(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"                 # build metadata
    r"$"
)

# Permission names: lowercase letters, digits, underscores, dots, hyphens.
# Must be non-empty and contain no whitespace.
_PERMISSION_RE = re.compile(r"^[a-z][a-z0-9._-]*$")


def _is_valid_semver(version: str) -> bool:
    """Return True if ``version`` is a valid semver 2.0 string."""
    return bool(_SEMVER_RE.match(version))


def _is_valid_permission(perm: str) -> bool:
    """Return True if ``perm`` is a valid permission scope string."""
    return bool(_PERMISSION_RE.match(perm))


# ── PluginManifestV2 ─────────────────────────────────────────────────


@dataclass
class PluginManifestV2:
    """Plugin manifest v2 — dataclass-based, no Pydantic dependency.

    Fields:
        name: Unique plugin identifier (e.g. ``"my-plugin"``).
        version: SemVer version string (e.g. ``"1.2.3"``).
        author: Author/organisation name.
        description: Short description of the plugin.
        min_harness_version: Minimum Harness version required
            (semver, e.g. ``"1.32.0"``). Empty string = v1 compat
            (no minimum declared).
        permissions: List of requested permission scopes. Each must
            be a non-empty lowercase identifier. Empty list = no
            scopes requested (v1 compat).
        signature: Ed25519 signature of the manifest (hex-encoded,
            128 chars). ``None`` = unsigned.
        public_key: Ed25519 public key (hex-encoded, 64 chars).
            ``None`` = no key provided.
        entry_point: Main plugin module path (e.g. ``"my_pkg.plugin"``).
        homepage: Optional project homepage URL.
        repository: Optional source repository URL.
        keywords: List of tags for discovery.
    """

    name: str
    version: str
    author: str
    description: str
    min_harness_version: str
    permissions: list[str]
    entry_point: str
    signature: str | None = None
    public_key: str | None = None
    homepage: str | None = None
    repository: str | None = None
    keywords: list[str] = field(default_factory=list)

    # ── Validation ───────────────────────────────────────────────

    def validate(self) -> None:
        """Validate manifest fields.

        Raises:
            ValueError: If ``version`` or ``min_harness_version`` is
                non-empty and not valid semver, or if any permission
                string is invalid.
        """
        # version is always required and must be valid semver.
        if not _is_valid_semver(self.version):
            raise ValueError(
                f"Invalid semver for version: {self.version!r}"
            )

        # min_harness_version: if non-empty, must be valid semver.
        # Empty = v1 compat (no minimum declared).
        if self.min_harness_version and not _is_valid_semver(
            self.min_harness_version
        ):
            raise ValueError(
                f"Invalid semver for min_harness_version: "
                f"{self.min_harness_version!r}"
            )

        # Permissions: if any are present, each must be valid.
        for perm in self.permissions:
            if not perm or not _is_valid_permission(perm):
                raise ValueError(
                    f"Invalid permission: {perm!r}. "
                    f"Must be non-empty, lowercase [a-z0-9._-]."
                )

        # Warn if signature is missing (unsigned manifests are still
        # allowed — the security guarantee is weakened).
        if self.signature is None:
            log.warning(
                "Plugin %s v%s has no signature — manifest integrity "
                "is not cryptographically verified.",
                self.name,
                self.version,
            )

    # ── Serialization ────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize the manifest to a plain dict.

        ``None`` values for optional fields (signature, public_key,
        homepage, repository) are preserved as ``None`` in the output.
        """
        return {
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "min_harness_version": self.min_harness_version,
            "permissions": list(self.permissions),
            "signature": self.signature,
            "public_key": self.public_key,
            "entry_point": self.entry_point,
            "homepage": self.homepage,
            "repository": self.repository,
            "keywords": list(self.keywords),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifestV2:
        """Deserialize a plain dict into a :class:`PluginManifestV2`.

        Missing optional fields get their defaults. Missing v2-specific
        mandatory fields (``min_harness_version``, ``permissions``) get
        empty sentinel values for backward compatibility with v1 manifests.

        Args:
            data: Dictionary with manifest fields. Unknown keys are
                silently ignored.

        Returns:
            A new :class:`PluginManifestV2` instance.
        """
        return cls(
            name=data["name"],
            version=data["version"],
            author=data["author"],
            description=data["description"],
            min_harness_version=data.get("min_harness_version", ""),
            permissions=list(data.get("permissions", [])),
            signature=data.get("signature"),
            public_key=data.get("public_key"),
            entry_point=data["entry_point"],
            homepage=data.get("homepage"),
            repository=data.get("repository"),
            keywords=list(data.get("keywords", [])),
        )

    # ── Backward compatibility ───────────────────────────────────

    def is_backward_compat_v1(self) -> bool:
        """Return ``True`` if this manifest has no v2-specific fields set.

        A manifest is considered v1-compatible when all v2-specific
        fields are at their sentinel values:
        - ``min_harness_version`` is empty
        - ``permissions`` is empty
        - ``signature`` is ``None``
        - ``public_key`` is ``None``

        Such a manifest can be safely consumed by a v1 loader that
        only understands ``PLUGIN_NAME`` / ``PLUGIN_VERSION``.
        """
        return (
            self.min_harness_version == ""
            and self.permissions == []
            and self.signature is None
            and self.public_key is None
        )
