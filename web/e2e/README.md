# E2E Tests — Harness Solomon Web UI

End-to-end tests for the Harness Admin web UI, powered by [Playwright](https://playwright.dev).

## Prerequisites

```bash
cd web
npm install
npx playwright install chromium
```

## Running tests

> Tests automatically skip when the dev server is not reachable, so the suite
> stays green in environments without a live backend.

### Start the dev server

```bash
npm run dev          # Vite on http://localhost:5173
```

The Harness backend should also be running on `:8765` for proxied `/api` and
`/auth` calls (some tests may exercise pages that require backend data).

### Headless (default)

```bash
npm run test:e2e
```

### Headed (watch the browser)

```bash
npm run test:e2e:headed
```

### Single spec file

```bash
npx playwright test e2e/home.spec.ts
npx playwright test e2e/navigation.spec.ts
npx playwright test e2e/settings.spec.ts
```

### HTML report

After a run, open the interactive report:

```bash
npx playwright show-report
```

## Browsers

Currently only **chromium** is installed. To add more browsers:

```bash
npx playwright install firefox webkit
```

Then add them to `projects` in `playwright.config.ts`:

```ts
projects: [
  { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  { name: "firefox",  use: { ...devices["Desktop Firefox"] } },
  { name: "webkit",   use: { ...devices["Desktop Safari"] } },
],
```

Run with a specific browser project:

```bash
npx playwright test --project=firefox
```

## Configuration

Key settings in `playwright.config.ts`:

| Setting | Value | Notes |
|---------|-------|-------|
| `testDir` | `./e2e` | All `*.spec.ts` files here |
| `baseURL` | `http://localhost:5173` | Vite dev server (override with `E2E_BASE_URL`) |
| `timeout` | 60s | Per-test hard limit |
| `retries` | 0 | No retries by default |
| `headless` | `true` | Override via `--headed` |
| `trace` | `retain-on-failure` | Debug failures with `npx playwright show-trace` |

To auto-start the dev server before tests, uncomment the `webServer` block in
`playwright.config.ts`.

## Test structure

```
e2e/
├── home.spec.ts         # Smoke: page load, title, sidebar visibility, root redirect
├── navigation.spec.ts   # Sidebar navigation: click each NavLink, verify route + heading
├── settings.spec.ts     # Settings page: version table, categories, values
└── README.md            # This file
```

### Conventions

- **Strict TypeScript**: all selectors are typed; no `any`.
- **Resilient skipping**: each spec checks dev-server reachability in
  `beforeEach` and calls `test.skip(...)` if unavailable — never `test.fail`.
- **Role-based selectors**: prefer `getByRole(...)` over CSS classes so tests
  survive styling refactors.
- **No backend mutation**: these are read-only smoke/navigation tests. Tests
  that create/edit data will live in separate `*.edit.spec.ts` files later.

## Debugging

```bash
# Run a single test with Playwright Inspector
npx playwright test e2e/home.spec.ts --debug

# View trace for a failed run
npx playwright show-trace test-results/.../trace.zip

# Run with visible browser + slow motion
npx playwright test --headed --workers=1
```

## Adding new tests

1. Create `e2e/<feature>.spec.ts`.
2. Import `{ expect, test }` from `@playwright/test`.
3. Wrap tests in `test.describe("<feature>", () => { ... })`.
4. Include the `isServerUp` + `test.skip` guard in `beforeEach`.
5. Use role-based locators (`getByRole`, `getByText`) over brittle CSS paths.
