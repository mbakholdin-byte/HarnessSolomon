/**
 * WI-02: Privacy Zones API — refactored to use shared ``APIClient``.
 *
 * Backend: ``/api/v1/privacy/zones`` (Phase 5.3 v1.25.0).
 * Requires ``privacy.read`` (GET) / ``privacy.write`` (POST/PUT/DELETE) scopes.
 */

import { api } from "./client";

export type ZoneAction = "block" | "redact" | "skip";

export interface PrivacyZone {
  id: string;
  pattern: string;
  action: ZoneAction;
  description: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface PrivacyZoneCreate {
  pattern: string;
  action: ZoneAction;
  description?: string;
  enabled?: boolean;
}

export interface PrivacyZoneUpdate {
  pattern?: string;
  action?: ZoneAction;
  description?: string;
  enabled?: boolean;
}

export interface PrivacyZoneListResponse {
  zones: PrivacyZone[];
  total: number;
}

const BASE = "/privacy/zones";

export const privacyZonesAPI = {
  /** List all REST-managed privacy zones (oldest first). */
  list(): Promise<PrivacyZoneListResponse> {
    return api.get<PrivacyZoneListResponse>(BASE);
  },

  /** Get a single zone by id. 404 if not found. */
  get(zoneId: string): Promise<PrivacyZone> {
    return api.get<PrivacyZone>(`${BASE}/${zoneId}`);
  },

  /** Create a new zone. Returns 201. */
  create(body: PrivacyZoneCreate): Promise<PrivacyZone> {
    return api.post<PrivacyZone>(BASE, body);
  },

  /** Update an existing zone (partial — only provided fields). */
  update(zoneId: string, body: PrivacyZoneUpdate): Promise<PrivacyZone> {
    return api.put<PrivacyZone>(`${BASE}/${zoneId}`, body);
  },

  /** Delete a zone. Returns 204 (``null``). */
  delete(zoneId: string): Promise<void> {
    return api.delete<void>(`${BASE}/${zoneId}`);
  },
};
