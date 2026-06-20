/**
 * Phase 5.3 v1.25.0 — API client for /api/v1/privacy/zones.
 *
 * Thin wrapper around fetch(). No external dependencies — works in
 * any React project (Next.js, CRA, Vite). The ``apiBase`` defaults
 * to ``/api/v1/privacy`` and can be overridden via the ``baseURL``
 * parameter to ``createPrivacyApiClient``.
 */

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

export interface PrivacyApiClient {
  list(): Promise<PrivacyZoneListResponse>;
  get(zoneId: string): Promise<PrivacyZone>;
  create(body: PrivacyZoneCreate): Promise<PrivacyZone>;
  update(zoneId: string, body: PrivacyZoneUpdate): Promise<PrivacyZone>;
  remove(zoneId: string): Promise<void>;
}

export function createPrivacyApiClient(
  baseURL: string = "/api/v1/privacy",
  getToken?: () => string | null,
): PrivacyApiClient {
  const headers = (extra?: Record<string, string>): HeadersInit => {
    const h: Record<string, string> = {
      "Content-Type": "application/json",
      ...extra,
    };
    const token = getToken?.();
    if (token) {
      h["Authorization"] = `Bearer ${token}`;
    }
    return h;
  };

  const handle = async <T>(res: Response): Promise<T> => {
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        detail = body.detail ?? detail;
      } catch {
        // ignore JSON parse errors
      }
      throw new Error(detail);
    }
    if (res.status === 204) {
      return undefined as unknown as T;
    }
    return res.json() as Promise<T>;
  };

  return {
    async list(): Promise<PrivacyZoneListResponse> {
      const res = await fetch(`${baseURL}/zones`, {
        method: "GET",
        headers: headers(),
      });
      return handle<PrivacyZoneListResponse>(res);
    },

    async get(zoneId: string): Promise<PrivacyZone> {
      const res = await fetch(`${baseURL}/zones/${zoneId}`, {
        method: "GET",
        headers: headers(),
      });
      return handle<PrivacyZone>(res);
    },

    async create(body: PrivacyZoneCreate): Promise<PrivacyZone> {
      const res = await fetch(`${baseURL}/zones`, {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(body),
      });
      return handle<PrivacyZone>(res);
    },

    async update(zoneId: string, body: PrivacyZoneUpdate): Promise<PrivacyZone> {
      const res = await fetch(`${baseURL}/zones/${zoneId}`, {
        method: "PUT",
        headers: headers(),
        body: JSON.stringify(body),
      });
      return handle<PrivacyZone>(res);
    },

    async remove(zoneId: string): Promise<void> {
      const res = await fetch(`${baseURL}/zones/${zoneId}`, {
        method: "DELETE",
        headers: headers(),
      });
      await handle<void>(res);
    },
  };
}
