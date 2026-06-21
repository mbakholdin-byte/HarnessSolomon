/**
 * E2E: Home / smoke test.
 *
 * Requirements before running:
 *   1. Start Vite dev server:  `npm run dev`
 *   2. Start Harness backend:  (listens on :8765, proxied via /api, /auth)
 *   3. Run:  `npm run test:e2e home`
 *
 * This test verifies that:
 *   - The web UI loads without uncaught console errors
 *   - Document title is present
 *   - The Sidebar with main navigation items is visible
 *
 * Uses test.skip when the dev server is not reachable so the suite
 * can run in CI / local without a live server and still report cleanly.
 */
import { expect, test, type Page } from "@playwright/test";

const DEV_SERVER = "http://localhost:5173";

/** Quick TCP-style reachability check for the dev server. */
async function isServerUp(page: Page): Promise<boolean> {
  try {
    const response = await page.request.get(DEV_SERVER, {
      timeout: 3_000,
      failOnStatusCode: false,
    });
    return response.ok();
  } catch {
    return false;
  }
}

test.describe("Home — smoke test", () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!(await isServerUp(page)), "Vite dev server not running on :5173");
  });

  test("page loads with no console errors and has a title", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));

    await page.goto("/");

    // Title must exist and be non-empty.
    const title = await page.title();
    expect(title.length).toBeGreaterThan(0);

    // No uncaught runtime errors.
    expect(errors, `Console errors: ${errors.join("; ")}`).toEqual([]);
  });

  test("main navigation sidebar is visible", async ({ page }) => {
    await page.goto("/");

    // Sidebar landmark.
    const sidebar = page.locator("aside").first();
    await expect(sidebar).toBeVisible();

    // At least the core navigation labels from NAV_ITEMS should be present.
    const expectedLabels = [
      "Privacy Zones",
      "Hooks",
      "Observability",
      "Audit",
      "Plugins",
      "Marketplace",
      "Settings",
    ];
    for (const label of expectedLabels) {
      await expect(page.getByText(label, { exact: true }).first()).toBeVisible();
    }
  });

  test("root redirects to default landing page", async ({ page }) => {
    await page.goto("/");
    // App.tsx redirects index → /privacy-zones.
    await expect(page).toHaveURL(/\/privacy-zones/);
  });
});
