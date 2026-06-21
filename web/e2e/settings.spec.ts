/**
 * E2E: Settings page content.
 *
 * Requirements before running:
 *   1. Start Vite dev server:  `npm run dev`
 *   2. Start Harness backend:  (:8765, proxied)
 *   3. Run:  `npm run test:e2e settings`
 *
 * This test verifies that the /settings page:
 *   - Renders the version info table
 *   - Shows core setting categories (General, API Keys, About)
 *   - Displays the app version value
 *
 * Uses test.skip when the dev server is not reachable.
 */
import { expect, test, type Page } from "@playwright/test";

const DEV_SERVER = "http://localhost:5173";

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

test.describe("Settings page", () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!(await isServerUp(page)), "Vite dev server not running on :5173");
  });

  test("renders version table with expected rows", async ({ page }) => {
    await page.goto("/settings");

    // Page heading.
    await expect(page.getByRole("heading", { level: 1, name: /settings/i })).toBeVisible();

    // The About section contains a <table> with version/build/stack/license rows.
    const table = page.locator("table").first();
    await expect(table).toBeVisible();

    // Expected setting labels in the version table.
    const labels = ["Version", "Build", "Stack", "License"];
    for (const label of labels) {
      await expect(table.getByText(label, { exact: true }).first()).toBeVisible();
    }
  });

  test("shows at least one setting value (version number)", async ({ page }) => {
    await page.goto("/settings");

    // App version is "1.0.0" per SettingsPage.tsx.
    const versionCell = page.getByText("1.0.0", { exact: true }).first();
    await expect(versionCell).toBeVisible();
  });

  test("displays core setting categories", async ({ page }) => {
    await page.goto("/settings");

    // Section headings: General, API Keys, About.
    const sections = ["General", "API Keys", "About"];
    for (const section of sections) {
      const heading = page.getByRole("heading", { level: 2, name: new RegExp(section, "i") });
      await expect(heading).toBeVisible();
    }
  });
});
