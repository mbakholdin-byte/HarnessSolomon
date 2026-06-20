/**
 * WI-02: Central API client.
 *
 * All API modules use the singleton ``api`` export. The client handles:
 *
 *   - Bearer token injection from ``localStorage``
 *   - ``/api/v1`` base URL (single source of truth)
 *   - 401 → redirect to ``/login`` + token wipe
 *   - Non-OK → ``APIError`` with status + parsed body
 *   - 204 → ``null``
 */

import { APIError } from "./types";

const AUTH_TOKEN_KEY = "auth_token";

type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export class APIClient {
  readonly baseURL: string;
  private readonly _getToken: () => string | null;

  constructor(
    baseURL: string = "/api/v1",
    getToken: () => string | null = () =>
      localStorage.getItem(AUTH_TOKEN_KEY),
  ) {
    this.baseURL = baseURL;
    this._getToken = getToken;
  }

  // ── public shortcuts ──────────────────────────────────────────────

  async get<T>(path: string): Promise<T> {
    return this.request<T>("GET", path);
  }

  async post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("POST", path, body);
  }

  async put<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("PUT", path, body);
  }

  async patch<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("PATCH", path, body);
  }

  async delete<T = void>(path: string): Promise<T> {
    return this.request<T>("DELETE", path);
  }

  // ── core request ──────────────────────────────────────────────────

  async request<T>(
    method: HttpMethod,
    path: string,
    body?: unknown,
  ): Promise<T> {
    const url = `${this.baseURL}${path}`;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const token = this._getToken();
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    const init: RequestInit = {
      method,
      headers,
    };
    if (body !== undefined && method !== "GET" && method !== "DELETE") {
      init.body = JSON.stringify(body);
    }

    let res: Response;
    try {
      res = await fetch(url, init);
    } catch (err) {
      throw new APIError(
        0,
        null,
        `Network error: ${err instanceof Error ? err.message : String(err)}`,
      );
    }

    // 401 → wipe token + redirect to login
    if (res.status === 401) {
      localStorage.removeItem(AUTH_TOKEN_KEY);
      // Only redirect in browser context (not during tests)
      if (typeof window !== "undefined") {
        window.location.href = "/login";
      }
      let detail = "Unauthorized";
      try {
        const b = await res.clone().json();
        detail = b.detail ?? detail;
      } catch {
        /* ignore parse failures */
      }
      throw new APIError(401, null, detail);
    }

    // 204 No Content → null
    if (res.status === 204) {
      return null as T;
    }

    if (!res.ok) {
      let parsed: unknown = null;
      let detail: string = `HTTP ${res.status}`;
      try {
        parsed = await res.clone().json();
        const bodyDetail = (parsed as Record<string, unknown>)?.detail;
        if (typeof bodyDetail === "string") {
          detail = bodyDetail;
        }
      } catch {
        try {
          detail = await res.clone().text();
        } catch {
          /* keep default */
        }
      }
      throw new APIError(res.status, parsed, detail);
    }

    // Happy path: parse JSON body
    return res.json() as Promise<T>;
  }
}

/** Singleton — all API modules import this instance. */
export const api = new APIClient();
