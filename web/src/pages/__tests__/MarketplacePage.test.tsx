/**
 * WI-07: MarketplacePage tests.
 *
 * Tests the plugin marketplace UI: rendering, search, detail modal,
 * signature badges, empty/loading states, and pagination.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MarketplacePage } from "../MarketplacePage";

/* ── Mock ────────────────────────────────────────────────────────── */

vi.mock("../../api/marketplace", () => ({
  listMarketplacePlugins: vi.fn(),
  getMarketplacePlugin: vi.fn(),
}));

import {
  listMarketplacePlugins,
  getMarketplacePlugin,
  type MarketplacePlugin,
} from "../../api/marketplace";

/* ── Fixtures ────────────────────────────────────────────────────── */

const mockPlugins: MarketplacePlugin[] = [
  {
    name: "memory-pack",
    version: "1.2.0",
    author: "Acme Corp",
    description: "Persistent memory plugin with vector search capabilities.",
    min_harness_version: "1.30.0",
    permissions: ["memory.read", "memory.write"],
    signature: "sha256:abc123...",
    public_key: "ed25519:AAAA...",
    entry_point: "memory_pack/main.py",
    homepage: "https://example.com/memory-pack",
    repository: "https://github.com/acme/memory-pack",
    keywords: ["memory", "vector", "persistence"],
  },
  {
    name: "community-logger",
    version: "0.9.0",
    author: "Community",
    description: "A simple logging plugin for observability.",
    min_harness_version: "1.28.0",
    permissions: ["logs.write"],
    signature: null,
    public_key: null,
    entry_point: "logger/main.py",
    homepage: null,
    repository: "https://github.com/community/logger",
    keywords: ["logging", "observability"],
  },
  {
    name: "code-sandbox",
    version: "2.1.0",
    author: "DevTools Inc",
    description: "Sandboxed code execution environment.",
    min_harness_version: "1.32.0",
    permissions: ["sandbox.run", "sandbox.read"],
    signature: "sha256:def456...",
    public_key: "ed25519:BBBB...",
    entry_point: "sandbox/main.py",
    homepage: null,
    repository: null,
    keywords: ["sandbox", "execution", "security"],
  },
  {
    name: "nocode-plugin",
    version: "0.1.0",
    author: "Starter",
    description: "Minimal plugin with no keywords.",
    min_harness_version: "1.30.0",
    permissions: [],
    signature: null,
    public_key: null,
    entry_point: "nocode/run.py",
    homepage: null,
    repository: null,
    keywords: [],
  },
];

const mockPluginDetail: MarketplacePlugin = {
  name: "memory-pack",
  version: "1.2.0",
  author: "Acme Corp",
  description: "Persistent memory plugin with vector search capabilities.",
  min_harness_version: "1.30.0",
  permissions: ["memory.read", "memory.write"],
  signature: "sha256:abc123...",
  public_key: "ed25519:AAAABBBBCCCCDDDDEEEEFFFF00001111...",
  entry_point: "memory_pack/main.py",
  homepage: "https://example.com/memory-pack",
  repository: "https://github.com/acme/memory-pack",
  keywords: ["memory", "vector", "persistence"],
};

const mockListResponse = (plugins: MarketplacePlugin[], total?: number) => ({
  plugins,
  total: total ?? plugins.length,
});

/* ── Tests ───────────────────────────────────────────────────────── */

describe("MarketplacePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // All mocks default to successful responses
    vi.mocked(listMarketplacePlugins).mockResolvedValue(
      mockListResponse(mockPlugins),
    );
    vi.mocked(getMarketplacePlugin).mockResolvedValue(mockPluginDetail);
  });

  /* ── 1. Renders plugin list ────────────────────────────────────── */

  it("renders plugin list", async () => {
    render(<MarketplacePage />);

    await waitFor(() => {
      expect(screen.getByText("memory-pack")).toBeInTheDocument();
    });

    expect(screen.getByText("community-logger")).toBeInTheDocument();
    expect(screen.getByText("code-sandbox")).toBeInTheDocument();
    expect(screen.getByText("v1.2.0")).toBeInTheDocument();
    expect(screen.getByText("by Acme Corp")).toBeInTheDocument();
    expect(
      screen.getByText(/Persistent memory plugin/),
    ).toBeInTheDocument();
  });

  /* ── 2. Search filters plugins ─────────────────────────────────── */

  it("search filters plugins", async () => {
    const user = userEvent.setup();

    // Mock list to return filtered results when keyword changes
    vi.mocked(listMarketplacePlugins)
      .mockResolvedValueOnce(mockListResponse(mockPlugins)) // initial
      .mockResolvedValueOnce(
        mockListResponse([mockPlugins[0]]), // filtered
      );

    render(<MarketplacePage />);

    // Wait for initial load
    await waitFor(() => {
      expect(screen.getByText("memory-pack")).toBeInTheDocument();
    });

    // Type keyword and click search
    const input = screen.getByTestId("marketplace-search-input");
    await user.clear(input);
    await user.type(input, "memory");
    await user.click(screen.getByTestId("marketplace-search-btn"));

    // Should filter to just memory-pack
    await waitFor(() => {
      expect(listMarketplacePlugins).toHaveBeenCalledWith(
        "memory",
        expect.any(Number),
        0,
      );
    });
  });

  /* ── 3. Click opens detail ─────────────────────────────────────── */

  it("click opens detail modal", async () => {
    const user = userEvent.setup();

    render(<MarketplacePage />);

    await waitFor(() => {
      expect(screen.getByText("memory-pack")).toBeInTheDocument();
    });

    // Click the first plugin card
    await user.click(screen.getByTestId("marketplace-card-memory-pack"));

    // Detail modal should appear
    await waitFor(() => {
      expect(screen.getByTestId("plugin-detail")).toBeInTheDocument();
    });

    // Detail content should be visible — check specific detail fields
    expect(screen.getByTestId("plugin-detail")).toBeInTheDocument();
    // "Min Harness" label only appears in detail, not in card
    expect(screen.getByText("Min Harness")).toBeInTheDocument();
    // "memory_pack/main.py" only appears in detail
    expect(
      screen.getByText("memory_pack/main.py"),
    ).toBeInTheDocument();
  });

  /* ── 4. Signature badge shown for signed plugins ───────────────── */

  it("signature badge shown for signed plugins", async () => {
    render(<MarketplacePage />);

    await waitFor(() => {
      expect(screen.getByText("memory-pack")).toBeInTheDocument();
    });

    // Signed plugins have the "Signed" footer badge with data-testid
    expect(screen.getByTestId("signed-memory-pack")).toHaveTextContent(
      "Signed",
    );
    expect(screen.getByTestId("signed-code-sandbox")).toHaveTextContent(
      "Signed",
    );
  });

  /* ── 5. Unsigned plugin shows warning ──────────────────────────── */

  it("unsigned plugin shows warning", async () => {
    render(<MarketplacePage />);

    await waitFor(() => {
      expect(screen.getByText("community-logger")).toBeInTheDocument();
    });

    expect(
      screen.getByTestId("unsigned-community-logger"),
    ).toHaveTextContent("Unsigned — use at your own risk");
    expect(
      screen.getByTestId("unsigned-nocode-plugin"),
    ).toHaveTextContent("Unsigned — use at your own risk");
  });

  /* ── 6. Empty state ────────────────────────────────────────────── */

  it("empty state shows placeholder", async () => {
    vi.mocked(listMarketplacePlugins).mockResolvedValue(
      mockListResponse([], 0),
    );

    render(<MarketplacePage />);

    await waitFor(() => {
      expect(screen.getByTestId("marketplace-empty")).toBeInTheDocument();
    });

    expect(
      screen.getByText(/No plugins available/),
    ).toBeInTheDocument();
  });

  /* ── 7. Pagination works ───────────────────────────────────────── */

  it("pagination works", async () => {
    const user = userEvent.setup();

    // Create 15 plugins to trigger pagination (limit = 12)
    const manyPlugins: MarketplacePlugin[] = Array.from(
      { length: 15 },
      (_, i) => ({
        ...mockPlugins[0],
        name: `plugin-${i + 1}`,
      }),
    );

    vi.mocked(listMarketplacePlugins).mockResolvedValue(
      mockListResponse(manyPlugins.slice(0, 12), 15),
    );

    render(<MarketplacePage />);

    await waitFor(() => {
      expect(screen.getByTestId("marketplace-pagination")).toBeInTheDocument();
    });

    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();

    // Next page — re-mock for page 2
    vi.mocked(listMarketplacePlugins).mockResolvedValue(
      mockListResponse(manyPlugins.slice(12), 15),
    );

    await user.click(screen.getByTestId("pagination-next"));

    await waitFor(() => {
      expect(listMarketplacePlugins).toHaveBeenCalledWith(
        undefined,
        expect.any(Number),
        12,
      );
    });
  });

  /* ── 8. Loading state ──────────────────────────────────────────── */

  it("loading state shows spinner", () => {
    vi.mocked(listMarketplacePlugins).mockReturnValue(
      new Promise(() => {
        /* never resolves */
      }),
    );

    render(<MarketplacePage />);

    expect(
      screen.getByTestId("marketplace-spinner"),
    ).toBeInTheDocument();
    expect(screen.getByText("Loading plugins...")).toBeInTheDocument();
  });
});
