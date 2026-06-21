"""Phase 7.4 WI-01 v1.32.0 — In-memory plugin marketplace catalogue.

Defines :class:`MarketplaceManager` — a local in-memory store of
available :class:`~harness.plugins.manifest_v2.PluginManifestV2`
manifests. In v1.32.0 this is a purely local catalogue; future
versions may add remote registry support.

Trust boundary (CRITICAL):
    This module imports ONLY from ``harness.plugins.manifest_v2``
    (same package). It does NOT import ``harness.agents`` or
    ``harness.server``.
"""
from __future__ import annotations

import logging

from harness.plugins.manifest_v2 import PluginManifestV2

__all__ = ["MarketplaceManager"]

log = logging.getLogger("harness.plugins.marketplace")


class MarketplaceManager:
    """In-memory plugin marketplace catalogue.

    In v1.32.0 this is a local catalogue. Future versions may
    add remote registry support.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, PluginManifestV2] = {}

    # ── Registration ────────────────────────────────────────────────

    def register(self, manifest: PluginManifestV2) -> None:
        """Register a plugin manifest in the marketplace.

        If a plugin with the same name already exists, it is
        overwritten (last-write-wins).  The caller is responsible
        for validating the manifest before registration.

        Args:
            manifest: A validated ``PluginManifestV2`` instance.
        """
        self._plugins[manifest.name] = manifest
        log.info(
            "Marketplace: registered %s v%s", manifest.name, manifest.version,
        )

    def unregister(self, name: str) -> bool:
        """Remove a plugin from the catalogue.

        Args:
            name: Plugin name to remove.

        Returns:
            ``True`` if the plugin was found and removed,
            ``False`` if it was not in the catalogue.
        """
        if name in self._plugins:
            del self._plugins[name]
            log.info("Marketplace: unregistered %s", name)
            return True
        return False

    # ── Lookup ──────────────────────────────────────────────────────

    def get(self, name: str) -> PluginManifestV2 | None:
        """Get a plugin manifest by name.

        Args:
            name: Plugin name.

        Returns:
            The ``PluginManifestV2`` if found, or ``None``.
        """
        return self._plugins.get(name)

    def list_plugins(
        self,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PluginManifestV2]:
        """List available plugins, optionally filtered by keyword.

        When ``keyword`` is provided, the result is filtered to
        plugins whose **name**, **description**, or **keywords**
        contain the keyword (case-insensitive substring match).

        Args:
            keyword: Optional filter string.
            limit: Maximum number of results (default 50).
            offset: Offset for pagination (default 0).

        Returns:
            A slice ``[offset:offset+limit]`` of matching manifests.
        """
        manifests = list(self._plugins.values())

        if keyword:
            kw_lower = keyword.lower()
            manifests = [
                m
                for m in manifests
                if kw_lower in m.name.lower()
                or kw_lower in m.description.lower()
                or any(kw_lower in k.lower() for k in m.keywords)
            ]

        # Sort by name for deterministic output.
        manifests.sort(key=lambda m: m.name)
        return manifests[offset : offset + limit]

    def search(self, query: str) -> list[PluginManifestV2]:
        """Search plugins by name, description, keywords.

        This is a convenience wrapper around :meth:`list_plugins`
        with the ``keyword`` parameter.

        Args:
            query: Search string (case-insensitive substring match
                against name, description, and keywords).

        Returns:
            All matching manifests (no pagination).
        """
        return self.list_plugins(keyword=query, limit=10_000, offset=0)

    def __len__(self) -> int:
        return len(self._plugins)
