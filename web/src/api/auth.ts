/**
 * WI-02: Auth helpers.
 *
 * The Harness backend uses Bearer token authentication (Phase 1.6).
 * Tokens are generated server-side via the CLI, not through a login
 * endpoint. The frontend stores the token in ``localStorage`` and
 * ``APIClient`` reads it on every request.
 *
 * A future ``POST /api/v1/auth/login`` endpoint (username + password
 * → token) is planned but not yet implemented. The ``login()`` function
 * below targets that planned surface.
 */

import { api } from "./client";

const AUTH_TOKEN_KEY = "auth_token";

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  token: string;
}

/**
 * Authenticate and store the token.
 *
 * Calls ``POST /api/v1/auth/login``. On success, stores the token
 * in ``localStorage`` and returns the response.
 *
 * **Note:** The backend does not yet expose a login endpoint (tokens are
 * CLI-generated). This function targets the planned REST surface.
 */
export async function login(
  username: string,
  password: string,
): Promise<LoginResponse> {
  const res = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(
      (body as Record<string, string>)?.detail ?? `Login failed (${res.status})`,
    );
  }
  const data: LoginResponse = await res.json();
  localStorage.setItem(AUTH_TOKEN_KEY, data.token);
  return data;
}

/** Clear stored token — the next API call will receive a 401 and redirect. */
export function logout(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY);
}

/** Check whether a token is present in ``localStorage``.
 *  Does NOT validate the token against the server. */
export function isAuthenticated(): boolean {
  return localStorage.getItem(AUTH_TOKEN_KEY) !== null;
}

/** Direct token setter — for use when the operator pastes a CLI-generated token. */
export function setToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_KEY, token);
}
