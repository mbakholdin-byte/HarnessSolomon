/**
 * Playwright configuration for Harness Solomon Web UI.
 *
 * The Vite dev server runs at http://localhost:5173 in development.
 * The Harness backend (proxied via /api and /auth) runs at http://localhost:8765.
 *
 * Base URL points to the Vite dev server since E2E tests exercise the UI.
 * Start the dev server with `npm run dev` before running E2E tests,
 * or use `webServer` config below to auto-start (commented out by default).
 */
import { defineConfig, devices } from "@playwright/test";

const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:5173";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [["list"], ["html", { open: "never" }]],
  timeout: 60_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: BASE_URL,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // Uncomment to auto-start Vite dev server before tests:
  // webServer: {
  //   command: "npm run dev",
  //   url: "http://localhost:5173",
  //   reuseExistingServer: !process.env.CI,
  //   timeout: 60_000,
  // },
});
