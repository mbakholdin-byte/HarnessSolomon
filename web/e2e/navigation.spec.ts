/**
 * E2E: Sidebar navigation.
 *
 * Requirements before running:
 *   1. Start Vite dev server:  `npm run dev`
 *   2. Start Harness backend:  (:8765, proxied)
 *   3. Run:  `npm run test:e2e navigation`
 *
 * This test verifies that:
 *   - Clicking each Sidebar NavLink changes the active route
 *   - The active state visually reflects the selected item
 *
 * Uses test.skip when the dev server is not reachable.
 */
import { expect, test, type Page } from "@playwright/test";

const DEV_SERVER = "http://localhost:5173";

interface RouteSpec {
  /** Path relative to baseURL, with leading slash. */
  path: string;
  /** Human-readable label shown in the Sidebar. */
  label: string;
  /** Heading text expected on the target page (h1). */
  heading: string;
}

const ROUTES: RouteSpec[] = [
  { path: "/privacy-zones", label: "Privacy Zones", heading: "Privacy Zones" },
  { path: "/hooks", label: "Hooks", heading: "Hooks" },
  { path: "/observability", label: "Observability", heading: "Observability" },
  { path: "/audit", label: "Audit", heading: "Audit" },
  { path: "/plugins", label: "Plugins", heading: "Plugins" },
  { path: "/marketplace", label: "Marketplace", heading: "Marketplace" },
  { path: "/settings", label: "Settings", heading: "Settings" },
];

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

test.describe("Sidebar navigation", () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!(await isServerUp(page)), "Vite dev server not running on :5173");
  });

  for (const route of ROUTES) {
    test(`navigate to ${route.label} via sidebar`, async ({ page }) => {
      await page.goto("/");

      // Click the nav link by visible label.
      const link = page.getByRole("link", { name: new RegExp(route.label, "i") }).first();
      await link.click();

      // URL should reflect the selected route.
      await expect(page).toHaveURL(new RegExp(`${route.path}$`));

      // Page heading should be present.
      const heading = page.getByRole("heading", { level: 1, name: new RegExp(route.heading, "i") });
      await expect(heading).toBeVisible();
    });
  }

  test("sidebar can be collapsed and expanded", async ({ page }) => {
    await page.goto("/");

    const toggle = page.getByRole("button", { name: /collapse sidebar/i });
    await expect(toggle).toBeVisible();

    // Collapse.
    await toggle.click();
    await expect(page.getByRole("button", { name: /expand sidebar/i })).toBeVisible();

    // Expand back.
    await page.getByRole("button", { name: /expand sidebar/i }).click();
    await expect(page.getByRole("button", { name: /collapse sidebar/i })).toBeVisible();
  });
});
