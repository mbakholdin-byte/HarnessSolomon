"""v1.32.0 — Tests for /api/v1/marketplace/* REST API.

Verifies:
  * GET /api/v1/marketplace/plugins — list + keyword + pagination
  * GET /api/v1/marketplace/plugins/{name} — detail
  * 404 for unknown plugin names
  * Unregister behaviour
  * Search endpoint

Uses ``httpx.AsyncClient`` with ``ASGITransport`` (no real HTTP server).
Lifespan events are triggered via ``asgi-lifespan`` (already in dev deps).

Run::

    pytest tests/test_marketplace_api.py -v --tb=short
"""
from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport

from harness.config import settings
from harness.server.app import create_app
from harness.plugins.marketplace import MarketplaceManager
from harness.plugins.manifest_v2 import PluginManifestV2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sample_manifest(
    name: str,
    version: str = "1.0.0",
    author: str = "Test Author",
    description: str = "A test plugin.",
    min_harness_version: str = "1.32.0",
    permissions: list[str] | None = None,
    entry_point: str = "test.plugin",
    keywords: list[str] | None = None,
    **kwargs: str | None,
) -> PluginManifestV2:
    """Create a minimal valid manifest for testing."""
    return PluginManifestV2(
        name=name,
        version=version,
        author=author,
        description=description,
        min_harness_version=min_harness_version,
        permissions=permissions or [],
        entry_point=entry_point,
        keywords=keywords or [],
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def marketplace() -> MarketplaceManager:
    """A clean ``MarketplaceManager`` for test isolation."""
    return MarketplaceManager()


@pytest.fixture
async def client(marketplace: MarketplaceManager):
    """An ``httpx.AsyncClient`` wired to the FastAPI app via ASGITransport.

    Overrides ``app.state.marketplace`` with the test fixture's
    clean manager so each test starts from an empty catalogue.
    Lifespan events are triggered explicitly via ``LifespanManager``
    so that the lifespan handler populates ``app.state.marketplace``
    before we override it.
    """
    settings.auth_required = False
    app = create_app()

    async with LifespanManager(app):
        # Override marketplace AFTER lifespan has run (so our clean
        # fixture takes precedence over lifespan-populated samples).
        app.state.marketplace = marketplace

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListPluginsEmpty:
    """test_list_plugins_empty — empty marketplace returns empty list."""

    async def test_list_plugins_empty(self, client: AsyncClient) -> None:
        """GET /api/v1/marketplace/plugins → empty list."""
        r = await client.get("/api/v1/marketplace/plugins")
        assert r.status_code == 200, f"unexpected status: {r.status_code}"
        body = r.json()
        assert body == {"plugins": [], "total": 0}


class TestRegisterAndList:
    """test_register_and_list — register 2 plugins → list shows both."""

    async def test_register_and_list(
        self, client: AsyncClient, marketplace: MarketplaceManager,
    ) -> None:
        """Register two plugins and verify they appear in the list."""
        marketplace.register(_make_sample_manifest(
            name="plugin-a",
            description="First test plugin.",
            keywords=["alpha"],
        ))
        marketplace.register(_make_sample_manifest(
            name="plugin-b",
            description="Second test plugin.",
            keywords=["beta"],
        ))

        r = await client.get("/api/v1/marketplace/plugins")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        names = {p["name"] for p in body["plugins"]}
        assert names == {"plugin-a", "plugin-b"}


class TestGetPluginDetail:
    """test_get_plugin_detail — GET detail returns full manifest."""

    async def test_get_plugin_detail(
        self, client: AsyncClient, marketplace: MarketplaceManager,
    ) -> None:
        """GET on a registered plugin returns its full manifest."""
        marketplace.register(_make_sample_manifest(
            name="full-plugin",
            version="1.2.3",
            author="Full Author",
            description="A detailed plugin.",
            min_harness_version="1.32.0",
            permissions=["tools.execute", "files.read"],
            entry_point="full.plugin",
            homepage="https://example.com",
            repository="https://github.com/example/full-plugin",
            keywords=["full", "test"],
        ))

        r = await client.get("/api/v1/marketplace/plugins/full-plugin")
        assert r.status_code == 200, f"unexpected status: {r.status_code}"
        body = r.json()
        assert body["name"] == "full-plugin"
        assert body["version"] == "1.2.3"
        assert body["author"] == "Full Author"
        assert body["description"] == "A detailed plugin."
        assert body["min_harness_version"] == "1.32.0"
        assert body["permissions"] == ["tools.execute", "files.read"]
        assert body["entry_point"] == "full.plugin"
        assert body["homepage"] == "https://example.com"
        assert body["repository"] == "https://github.com/example/full-plugin"
        assert body["keywords"] == ["full", "test"]
        # Optional fields that were not set.
        assert body["signature"] is None
        assert body["public_key"] is None


class TestGetPluginNotFound:
    """test_get_plugin_not_found — unknown plugin returns 404."""

    async def test_get_plugin_not_found(self, client: AsyncClient) -> None:
        """GET on a nonexistent plugin returns 404."""
        r = await client.get("/api/v1/marketplace/plugins/nonexistent")
        assert r.status_code == 404


class TestListWithKeywordFilter:
    """test_list_with_keyword_filter — keyword filters by name/description/keywords."""

    async def test_list_with_keyword_filter(
        self, client: AsyncClient, marketplace: MarketplaceManager,
    ) -> None:
        """Keyword filter matches across name, description, and keywords."""
        marketplace.register(_make_sample_manifest(
            name="git-helper",
            description="Git integration tools.",
            keywords=["git", "vcs"],
        ))
        marketplace.register(_make_sample_manifest(
            name="sql-tool",
            description="Database query tool.",
            keywords=["sql", "database"],
        ))
        marketplace.register(_make_sample_manifest(
            name="code-formatter",
            description="Auto-format code on save.",
            keywords=["formatting"],
        ))

        # Match by name.
        r = await client.get("/api/v1/marketplace/plugins?keyword=git")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["plugins"][0]["name"] == "git-helper"

        # Match by description.
        r = await client.get("/api/v1/marketplace/plugins?keyword=database")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["plugins"][0]["name"] == "sql-tool"

        # Match by keyword tag.
        r = await client.get("/api/v1/marketplace/plugins?keyword=formatting")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["plugins"][0]["name"] == "code-formatter"

        # No matches.
        r = await client.get("/api/v1/marketplace/plugins?keyword=xyzzy")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0


class TestListWithPagination:
    """test_list_with_pagination — limit + offset work correctly."""

    async def test_list_with_pagination(
        self, client: AsyncClient, marketplace: MarketplaceManager,
    ) -> None:
        """limit and offset paginate the result set."""
        for i in range(5):
            marketplace.register(_make_sample_manifest(
                name=f"plugin-{i:02d}",
                description=f"Plugin number {i}.",
            ))

        # First page: limit=2, offset=0.
        r = await client.get("/api/v1/marketplace/plugins?limit=2&offset=0")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 5
        assert len(body["plugins"]) == 2
        assert body["plugins"][0]["name"] == "plugin-00"
        assert body["plugins"][1]["name"] == "plugin-01"

        # Second page: limit=2, offset=2.
        r = await client.get("/api/v1/marketplace/plugins?limit=2&offset=2")
        assert r.status_code == 200
        body = r.json()
        assert len(body["plugins"]) == 2
        assert body["plugins"][0]["name"] == "plugin-02"
        assert body["plugins"][1]["name"] == "plugin-03"

        # Last page: limit=2, offset=4.
        r = await client.get("/api/v1/marketplace/plugins?limit=2&offset=4")
        assert r.status_code == 200
        body = r.json()
        assert len(body["plugins"]) == 1
        assert body["plugins"][0]["name"] == "plugin-04"


class TestUnregisterPlugin:
    """test_unregister_plugin — remove from catalogue → 404."""

    async def test_unregister_plugin(
        self, client: AsyncClient, marketplace: MarketplaceManager,
    ) -> None:
        """After unregistering, GET returns 404."""
        marketplace.register(_make_sample_manifest(name="to-remove"))

        # Should be visible before unregister.
        r = await client.get("/api/v1/marketplace/plugins/to-remove")
        assert r.status_code == 200

        # Remove it.
        removed = marketplace.unregister("to-remove")
        assert removed is True

        # Now 404.
        r = await client.get("/api/v1/marketplace/plugins/to-remove")
        assert r.status_code == 404

        # Unregister again → False (not found).
        assert marketplace.unregister("to-remove") is False


class TestSearchPlugins:
    """test_search_plugins — search across multiple fields."""

    async def test_search_plugins(
        self, client: AsyncClient, marketplace: MarketplaceManager,
    ) -> None:
        """search() returns matches across name, description, keywords."""
        marketplace.register(_make_sample_manifest(
            name="alpha",
            description="The first tool.",
            keywords=["one", "first"],
        ))
        marketplace.register(_make_sample_manifest(
            name="beta",
            description="A tool for beta testing.",
            keywords=["two", "testing"],
        ))
        marketplace.register(_make_sample_manifest(
            name="gamma",
            description="Gamma radiation analyzer.",
            keywords=["three", "science"],
        ))

        # search() via the marketplace manager directly (unit-test style)
        results = marketplace.search("beta")
        assert len(results) == 1
        assert results[0].name == "beta"

        # search matches keyword tag
        results = marketplace.search("science")
        assert len(results) == 1
        assert results[0].name == "gamma"

        # search matches description
        results = marketplace.search("tool")
        assert len(results) == 2
        names = {m.name for m in results}
        assert names == {"alpha", "beta"}

        # search no match
        results = marketplace.search("nonexistent")
        assert len(results) == 0
