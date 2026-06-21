/**
 * WI-07: Marketplace API client.
 *
 * Backend: ``/api/v1/marketplace/plugins`` (Phase 7.4 v1.32.0).
 * Provides listing and detail retrieval of published plugins.
 */

import { api } from "./client";

/** A plugin published in the marketplace. */
export interface MarketplacePlugin {
  name: string;
  version: string;
  author: string;
  description: string;
  min_harness_version: string;
  permissions: string[];
  signature: string | null;
  public_key: string | null;
  entry_point: string;
  homepage: string | null;
  repository: string | null;
  keywords: string[];
}

/** Paginated list response from the marketplace. */
export interface MarketplaceListResponse {
  plugins: MarketplacePlugin[];
  total: number;
}

const BASE = "/marketplace/plugins";

/** List marketplace plugins, optionally filtered by keyword with pagination. */
export async function listMarketplacePlugins(
  keyword?: string,
  limit?: number,
  offset?: number,
): Promise<MarketplaceListResponse> {
  const params = new URLSearchParams();
  if (keyword) params.set("keyword", keyword);
  if (limit !== undefined) params.set("limit", String(limit));
  if (offset !== undefined) params.set("offset", String(offset));

  const qs = params.toString();
  const path = qs ? `${BASE}?${qs}` : BASE;
  return api.get<MarketplaceListResponse>(path);
}

/** Get a single plugin's full manifest by name. */
export async function getMarketplacePlugin(
  name: string,
): Promise<MarketplacePlugin> {
  return api.get<MarketplacePlugin>(
    `${BASE}/${encodeURIComponent(name)}`,
  );
}
