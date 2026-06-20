/**
 * WI-05: Auth Integration tests.
 *
 * 1. LoginPage renders form with username/password/submit
 * 2. LoginPage calls login on submit
 * 3. AuthGuard redirects to /login when not authenticated
 * 4. AuthGuard renders children when authenticated
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { LoginPage } from "../../pages/LoginPage";
import { AuthGuard } from "../AuthGuard";

// ── helpers ──────────────────────────────────────────────────────────

/** Wrap a component in MemoryRouter for tests that need routing context. */
function renderWithRouter(
  ui: JSX.Element,
  { initialRoute = "/" }: { initialRoute?: string } = {},
) {
  return render(
    <MemoryRouter initialEntries={[initialRoute]}>{ui}</MemoryRouter>,
  );
}

// ── setup / teardown ─────────────────────────────────────────────────

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ── 1. LoginPage renders form ────────────────────────────────────────

describe("LoginPage", () => {
  it("1. renders form with username, password, and submit button", () => {
    renderWithRouter(<LoginPage />);

    expect(
      screen.getByRole("textbox", { name: /username/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /sign in/i }),
    ).toBeInTheDocument();
  });

  // ── 2. LoginPage calls login on submit ────────────────────────────

  it("2. calls login on submit", async () => {
    const mockToken = "test-token-abc";
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ token: mockToken }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    // Provide a spy on localStorage so we can verify setItem later.
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");

    renderWithRouter(
      <Routes>
        <Route path="/" element={<p>Home</p>} />
        <Route path="/login" element={<LoginPage />} />
      </Routes>,
      { initialRoute: "/login" },
    );

    fireEvent.change(screen.getByRole("textbox", { name: /username/i }), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      // Verify fetch was called with correct args
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      const [url, init] = fetchSpy.mock.calls[0];
      expect(url).toBe("/api/v1/auth/login");
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body).toEqual({ username: "admin", password: "secret" });

      // Token stored → redirect → Home page rendered
      expect(setItemSpy).toHaveBeenCalledWith("auth_token", mockToken);
      expect(screen.getByText("Home")).toBeInTheDocument();
    });
  });
});

// ── 3–4. AuthGuard ───────────────────────────────────────────────────

describe("AuthGuard", () => {
  it("3. redirects to /login when not authenticated", () => {
    // No token in localStorage → isAuthenticated() === false
    renderWithRouter(
      <Routes>
        <Route path="/login" element={<p>Login Page</p>} />
        <Route
          path="/dashboard"
          element={
            <AuthGuard>
              <p>Protected Content</p>
            </AuthGuard>
          }
        />
      </Routes>,
      { initialRoute: "/dashboard" },
    );

    // Should redirect to /login, not render protected content
    expect(screen.getByText("Login Page")).toBeInTheDocument();
    expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
  });

  it("4. renders children when authenticated", () => {
    // Set a token → isAuthenticated() === true
    localStorage.setItem("auth_token", "valid-token");

    renderWithRouter(
      <Routes>
        <Route path="/login" element={<p>Login Page</p>} />
        <Route
          path="/dashboard"
          element={
            <AuthGuard>
              <p>Protected Content</p>
            </AuthGuard>
          }
        />
      </Routes>,
      { initialRoute: "/dashboard" },
    );

    expect(screen.getByText("Protected Content")).toBeInTheDocument();
    expect(screen.queryByText("Login Page")).not.toBeInTheDocument();
  });
});
