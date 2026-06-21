/**
 * WI-07: Marketplace page — browse and install plugins.
 *
 * Lists plugins from the plugin marketplace with search, detail view,
 * and install action (UI-only for MVP).
 */

import { useState, useEffect, useCallback } from "react";
import {
  listMarketplacePlugins,
  getMarketplacePlugin,
  type MarketplacePlugin,
} from "../api/marketplace";
import { APIError } from "../api/types";
import { Modal, Badge } from "../components";
import styles from "./MarketplacePage.module.css";

/* ── Constants ────────────────────────────────────────────────────── */

const DEFAULT_LIMIT = 12;

/* ── Helpers ─────────────────────────────────────────────────────── */

/** Derive badge variant from signature presence. */
function signatureVariant(
  sig: string | null,
): "success" | "warning" {
  return sig ? "success" : "warning";
}

/* ── Component ───────────────────────────────────────────────────── */

export function MarketplacePage(): JSX.Element {
  // List state
  const [plugins, setPlugins] = useState<MarketplacePlugin[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Search / pagination
  const [keyword, setKeyword] = useState("");
  const [appliedKeyword, setAppliedKeyword] = useState("");
  const [offset, setOffset] = useState(0);

  // Detail modal
  const [selectedPlugin, setSelectedPlugin] =
    useState<MarketplacePlugin | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  // Install state
  const [installing, setInstalling] = useState<string | null>(null);

  /* ── Data fetching ─────────────────────────────────────────────── */

  const fetchPlugins = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await listMarketplacePlugins(
        appliedKeyword || undefined,
        DEFAULT_LIMIT,
        offset,
      );
      setPlugins(data.plugins);
      setTotal(data.total);
    } catch (err) {
      setError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load marketplace plugins",
      );
    } finally {
      setLoading(false);
    }
  }, [appliedKeyword, offset]);

  useEffect(() => {
    void fetchPlugins();
  }, [fetchPlugins]);

  /* ── Search ────────────────────────────────────────────────────── */

  const handleSearch = (): void => {
    setOffset(0);
    setAppliedKeyword(keyword);
  };

  const handleKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === "Enter") {
      handleSearch();
    }
  };

  const clearSearch = (): void => {
    setKeyword("");
    setOffset(0);
    setAppliedKeyword("");
  };

  /* ── Detail modal ──────────────────────────────────────────────── */

  const openDetail = async (plugin: MarketplacePlugin): Promise<void> => {
    setSelectedPlugin(plugin);
    setDetailLoading(true);
    setDetailError(null);
    try {
      const full = await getMarketplacePlugin(plugin.name);
      setSelectedPlugin(full);
    } catch (err) {
      // Fall back to list data on detail failure
      setDetailError(
        err instanceof Error ? err.message : "Failed to load details",
      );
    } finally {
      setDetailLoading(false);
    }
  };

  const closeDetail = (): void => {
    setSelectedPlugin(null);
    setDetailError(null);
  };

  /* ── Install (UI-only for MVP) ─────────────────────────────────── */

  const handleInstall = async (plugin: MarketplacePlugin): Promise<void> => {
    setInstalling(plugin.name);
    // Placeholder — POST /api/v1/plugins/install not yet available
    await new Promise((resolve) => setTimeout(resolve, 600));
    setInstalling(null);
  };

  /* ── Pagination ────────────────────────────────────────────────── */

  const totalPages = Math.max(1, Math.ceil(total / DEFAULT_LIMIT));
  const currentPage = Math.floor(offset / DEFAULT_LIMIT) + 1;

  const goPrev = (): void => {
    if (offset >= DEFAULT_LIMIT) {
      setOffset(offset - DEFAULT_LIMIT);
    }
  };

  const goNext = (): void => {
    if (offset + DEFAULT_LIMIT < total) {
      setOffset(offset + DEFAULT_LIMIT);
    }
  };

  /* ── Render ────────────────────────────────────────────────────── */

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Marketplace</h1>
      </div>

      {/* Search bar */}
      <div className={styles.searchBar}>
        <input
          className={styles.searchInput}
          type="text"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Search plugins by keyword..."
          data-testid="marketplace-search-input"
        />
        <button
          className={styles.searchBtn}
          onClick={handleSearch}
          data-testid="marketplace-search-btn"
        >
          Search
        </button>
        {appliedKeyword && (
          <button
            className={styles.clearBtn}
            onClick={clearSearch}
            data-testid="marketplace-clear-btn"
          >
            Clear
          </button>
        )}
      </div>

      {/* Error */}
      {error && <div className={styles.errorBlock}>{error}</div>}

      {/* Loading */}
      {loading ? (
        <div className={styles.loading}>
          <div className={styles.spinner} data-testid="marketplace-spinner" />
          <span>Loading plugins...</span>
        </div>
      ) : plugins.length === 0 ? (
        /* Empty state */
        <div className={styles.empty} data-testid="marketplace-empty">
          {appliedKeyword
            ? `No plugins found for "${appliedKeyword}".`
            : "No plugins available in the marketplace."}
        </div>
      ) : (
        <>
          {/* Plugin grid */}
          <div className={styles.grid} data-testid="marketplace-grid">
            {plugins.map((plugin) => (
              <div
                key={plugin.name}
                className={styles.card}
                onClick={() => void openDetail(plugin)}
                data-testid={`marketplace-card-${plugin.name}`}
              >
                <div className={styles.cardHeader}>
                  <span className={styles.cardName}>{plugin.name}</span>
                  <Badge variant={signatureVariant(plugin.signature)}>
                    {plugin.signature ? "Signed" : "Unsigned"}
                  </Badge>
                </div>

                <div className={styles.cardMeta}>
                  <span className={styles.cardVersion}>
                    v{plugin.version}
                  </span>
                  <span className={styles.cardAuthor}>
                    by {plugin.author}
                  </span>
                </div>

                <p className={styles.cardDesc}>{plugin.description}</p>

                {plugin.keywords.length > 0 && (
                  <div className={styles.cardKeywords}>
                    {plugin.keywords.map((kw) => (
                      <span key={kw} className={styles.keywordTag}>
                        {kw}
                      </span>
                    ))}
                  </div>
                )}

                <div className={styles.cardFooter}>
                  {plugin.signature ? (
                    <span
                      className={styles.signedBadge}
                      data-testid={`signed-${plugin.name}`}
                    >
                      Signed
                    </span>
                  ) : (
                    <span
                      className={styles.unsignedWarning}
                      data-testid={`unsigned-${plugin.name}`}
                    >
                      Unsigned — use at your own risk
                    </span>
                  )}
                  <button
                    className={styles.installBtn}
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleInstall(plugin);
                    }}
                    disabled={installing === plugin.name}
                    data-testid={`install-btn-${plugin.name}`}
                  >
                    {installing === plugin.name ? "Installing..." : "Install"}
                  </button>
                </div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className={styles.pagination} data-testid="marketplace-pagination">
              <button
                className={styles.pageBtn}
                onClick={goPrev}
                disabled={offset === 0}
                data-testid="pagination-prev"
              >
                Previous
              </button>
              <span className={styles.pageInfo}>
                Page {currentPage} of {totalPages}
              </span>
              <button
                className={styles.pageBtn}
                onClick={goNext}
                disabled={offset + DEFAULT_LIMIT >= total}
                data-testid="pagination-next"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}

      {/* ── Detail Modal ──────────────────────────────────────────── */}
      <Modal
        open={selectedPlugin !== null}
        onClose={closeDetail}
        title={selectedPlugin ? selectedPlugin.name : "Plugin Details"}
      >
        {detailLoading ? (
          <div className={styles.loading}>Loading details...</div>
        ) : detailError ? (
          <div className={styles.errorBlock}>{detailError}</div>
        ) : selectedPlugin ? (
          <div className={styles.detail} data-testid="plugin-detail">
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Version</span>
              <span className={styles.detailValue}>
                {selectedPlugin.version}
              </span>
            </div>
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Author</span>
              <span className={styles.detailValue}>
                {selectedPlugin.author}
              </span>
            </div>
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Description</span>
              <span className={styles.detailValue}>
                {selectedPlugin.description}
              </span>
            </div>
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Min Harness</span>
              <span className={styles.detailValue}>
                {selectedPlugin.min_harness_version}
              </span>
            </div>
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Entry Point</span>
              <span className={styles.detailValue}>
                {selectedPlugin.entry_point}
              </span>
            </div>

            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Signature</span>
              <span className={styles.detailValue}>
                {selectedPlugin.signature ? (
                  <Badge variant="success">Signed</Badge>
                ) : (
                  <Badge variant="warning">Unsigned</Badge>
                )}
              </span>
            </div>

            {selectedPlugin.public_key && (
              <div className={styles.detailRow}>
                <span className={styles.detailLabel}>Public Key</span>
                <span className={styles.detailValue}>
                  <code className={styles.code}>
                    {selectedPlugin.public_key.slice(0, 40)}...
                  </code>
                </span>
              </div>
            )}

            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Permissions</span>
              <span className={styles.detailValue}>
                {selectedPlugin.permissions.length > 0
                  ? selectedPlugin.permissions.join(", ")
                  : "None"}
              </span>
            </div>

            {selectedPlugin.keywords.length > 0 && (
              <div className={styles.detailRow}>
                <span className={styles.detailLabel}>Keywords</span>
                <span className={styles.detailValue}>
                  {selectedPlugin.keywords.join(", ")}
                </span>
              </div>
            )}

            {selectedPlugin.homepage && (
              <div className={styles.detailRow}>
                <span className={styles.detailLabel}>Homepage</span>
                <span className={styles.detailValue}>
                  <a
                    href={selectedPlugin.homepage}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={styles.link}
                  >
                    {selectedPlugin.homepage}
                  </a>
                </span>
              </div>
            )}

            {selectedPlugin.repository && (
              <div className={styles.detailRow}>
                <span className={styles.detailLabel}>Repository</span>
                <span className={styles.detailValue}>
                  <a
                    href={selectedPlugin.repository}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={styles.link}
                  >
                    {selectedPlugin.repository}
                  </a>
                </span>
              </div>
            )}

            <div className={styles.detailActions}>
              <button
                className={styles.installBtn}
                onClick={() => void handleInstall(selectedPlugin)}
                disabled={installing === selectedPlugin.name}
                data-testid="detail-install-btn"
              >
                {installing === selectedPlugin.name
                  ? "Installing..."
                  : "Install"}
              </button>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}

export default MarketplacePage;
