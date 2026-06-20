/**
 * WI-05: Audit API — admin audit log with date filtering and download.
 *
 * Endpoints:
 *   GET /api/v1/audit?from=&to=&format=&limit=&offset=   — paginated list
 *   GET /api/v1/audit?from=&to=&format=json|csv           — blob download
 */

import { api } from "./client";
import type { Page } from "./types";

/** A single entry in the admin audit log. */
export interface AuditEntry {
  id: string;
  timestamp: string;
  event_type: string;
  source: string;
  message: string;
}

/** Query parameters for the audit list/download endpoint. */
export interface AuditParams {
  from?: string;
  to?: string;
  format?: "json" | "csv";
  limit?: number;
  offset?: number;
}

const AUTH_TOKEN_KEY = "auth_token";

function buildQuery(params: AuditParams): string {
  const filtered: Record<string, string> = {};
  if (params.from) filtered.from = params.from;
  if (params.to) filtered.to = params.to;
  if (params.format) filtered.format = params.format;
  if (params.limit !== undefined) filtered.limit = String(params.limit);
  if (params.offset !== undefined) filtered.offset = String(params.offset);
  const qs = new URLSearchParams(filtered).toString();
  return qs ? `?${qs}` : "";
}

export const auditAPI = {
  /** Paginated list of audit entries. */
  async list(params: AuditParams = {}): Promise<Page<AuditEntry>> {
    return api.get<Page<AuditEntry>>(`/audit${buildQuery(params)}`);
  },

  /** Download audit log as a blob (CSV or JSON).
   *  Uses raw fetch to avoid JSON parsing. */
  async download(params: AuditParams & { format: "json" | "csv" }): Promise<Blob> {
    const qs = buildQuery(params);
    const token = localStorage.getItem(AUTH_TOKEN_KEY) || "";
    const res = await fetch(`/api/v1/audit${qs}`, {
      headers: {
        Authorization: token ? `Bearer ${token}` : "",
      },
    });
    if (!res.ok) {
      throw new Error(`Download failed: HTTP ${res.status}`);
    }
    return res.blob();
  },
};
