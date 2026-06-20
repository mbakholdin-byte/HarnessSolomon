/**
 * WI-02: API Client tests.
 *
 * Uses Vitest with ``vi.fn()`` for fetch mocking. All tests use the
 * singleton ``api`` instance; each resets ``localStorage`` and mocks
 * between cases.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { api } from "../client";
import { APIError } from "../types";
import { privacyZonesAPI } from "../privacy";
import { hooksAPI } from "../hooks";
import { pluginsAPI } from "../plugins";
import { login, logout, isAuthenticated, setToken } from "../auth";

// ── helpers ──────────────────────────────────────────────────────────

/** Captured fetch call info. Populated by ``installFetchSpy``. */
interface CapturedCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

let _captured: CapturedCall | null = null;

function installFetchSpy(
  status: number = 200,
  body: unknown = { ok: true },
  headers: Record<string, string> = { "Content-Type": "application/json" },
): void {
  _captured = null;
  vi.spyOn(globalThis, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      _captured = {
        url,
        method: init?.method ?? "GET",
        headers: (init?.headers as Record<string, string>) ?? {},
        body: init?.body ? JSON.parse(init.body as string) : undefined,
      };

      // jsdom rejects ``new Response(body, { status: 204 })`` —
      // a 204 MUST have null body per the Fetch spec.
      if (status === 204) {
        return new Response(null, { status: 204 });
      }
      return new Response(JSON.stringify(body), { status, headers });
    },
  );
}

// ── setup / teardown ─────────────────────────────────────────────────

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  _captured = null;
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ── tests ────────────────────────────────────────────────────────────

describe("APIClient", () => {
  // ── 1. Bearer token in headers ─────────────────────────────────────

  it("1. Bearer token in headers", async () => {
    localStorage.setItem("auth_token", "test-token-123");
    installFetchSpy();

    await api.get("/test");

    expect(_captured).not.toBeNull();
    expect(_captured!.headers["Authorization"]).toBe("Bearer test-token-123");
    expect(_captured!.url).toContain("/api/v1/test");
    expect(_captured!.method).toBe("GET");
  });

  // ── 2. 401 → redirect + token cleared ──────────────────────────────

  it("2. 401 → redirect + token cleared", async () => {
    localStorage.setItem("auth_token", "expired-token");
    installFetchSpy(401, { detail: "invalid or revoked token" });

    // jsdom's window.location is non-configurable — we can't spy on
    // the href setter. Instead we verify the token is wiped AND the
    // error is thrown. The redirect itself is a browser concern.
    await expect(api.get("/test")).rejects.toThrow(APIError);
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  // ── 3. 4xx/5xx → APIError ──────────────────────────────────────────

  it("3a. 403 → APIError with status and detail", async () => {
    installFetchSpy(403, { detail: "missing required scope: agents.read" });

    try {
      await api.get("/agents/jobs");
      expect.fail("Expected APIError");
    } catch (err) {
      expect(err).toBeInstanceOf(APIError);
      const apiErr = err as APIError;
      expect(apiErr.status).toBe(403);
      expect(apiErr.message).toContain("missing required scope");
    }
  });

  it("3b. 500 → APIError", async () => {
    installFetchSpy(500, { detail: "Internal server error" });

    await expect(api.get("/crash")).rejects.toMatchObject({
      status: 500,
      message: expect.stringContaining("Internal server error"),
    });
  });

  // ── 4. 204 → null ──────────────────────────────────────────────────

  it("4. 204 → null", async () => {
    installFetchSpy(204);

    const result = await api.delete("/privacy/zones/abc123");
    expect(result).toBeNull();
  });
});

// ── Privacy zones URL formation ──────────────────────────────────────

describe("Privacy Zones API", () => {
  it("5. privacy zones endpoints URL formation", async () => {
    const calls: { method: string; url: string }[] = [];

    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        const method = init?.method ?? "GET";
        calls.push({ method, url });

        // DELETE returns 204; others 200.
        const status = method === "DELETE" ? 204 : 200;
        return new Response(status === 204 ? null : JSON.stringify({ zones: [], total: 0 }), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      },
    );

    await privacyZonesAPI.list();
    await privacyZonesAPI.get("zone-1");
    await privacyZonesAPI.create({ pattern: "**/.env", action: "block" });
    await privacyZonesAPI.update("zone-1", { enabled: false });
    await privacyZonesAPI.delete("zone-1");

    expect(calls).toEqual([
      { method: "GET", url: "/api/v1/privacy/zones" },
      { method: "GET", url: "/api/v1/privacy/zones/zone-1" },
      { method: "POST", url: "/api/v1/privacy/zones" },
      { method: "PUT", url: "/api/v1/privacy/zones/zone-1" },
      { method: "DELETE", url: "/api/v1/privacy/zones/zone-1" },
    ]);
  });
});

// ── Hooks PATCH disable ──────────────────────────────────────────────

describe("Hooks API", () => {
  it("6. hooks PATCH disable", async () => {
    installFetchSpy(200, { enabled: false });

    await hooksAPI.updateConfig({ enabled: false, audit_log: false });

    expect(_captured).not.toBeNull();
    expect(_captured!.method).toBe("PATCH");
    expect(_captured!.url).toBe("/api/v1/hooks/config");
    expect(_captured!.body).toEqual({ enabled: false, audit_log: false });
  });
});

// ── Plugins enable/disable ───────────────────────────────────────────

describe("Plugins API", () => {
  it("7. plugins enable/disable", async () => {
    const calls: { method: string; url: string }[] = [];

    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        calls.push({ method: init?.method ?? "GET", url });
        return new Response(JSON.stringify({ name: "p", enabled: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    );

    await pluginsAPI.enable("my-plugin");
    await pluginsAPI.disable("other-plugin");

    expect(calls).toEqual([
      { method: "POST", url: "/api/v1/plugins/my-plugin/enable" },
      { method: "POST", url: "/api/v1/plugins/other-plugin/disable" },
    ]);
  });
});

// ── Auth login returns token ─────────────────────────────────────────

describe("Auth", () => {
  it("8a. auth login returns token and stores it", async () => {
    const mockToken = "cli-generated-token-abc";
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ token: mockToken }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    // login() calls fetch directly, bypassing APIClient.
    const result = await login("admin", "secret");

    expect(result.token).toBe(mockToken);
    expect(localStorage.getItem("auth_token")).toBe(mockToken);
  });

  it("8b. logout clears token", () => {
    localStorage.setItem("auth_token", "some-token");
    logout();
    expect(localStorage.getItem("auth_token")).toBeNull();
  });

  it("8c. isAuthenticated reflects token presence", () => {
    expect(isAuthenticated()).toBe(false);
    setToken("new-token");
    expect(isAuthenticated()).toBe(true);
    logout();
    expect(isAuthenticated()).toBe(false);
  });
});
