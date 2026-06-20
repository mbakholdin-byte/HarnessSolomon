/**
 * WI-05: AuditPage tests.
 *
 * Tests: renders date range picker + format selector, date filter applied
 * correctly, CSV download triggers blob save, error state displayed.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AuditPage } from "../AuditPage";

/* ── Mock auditAPI ────────────────────────────────────────────────── */

const mockList = vi.fn();
const mockDownload = vi.fn();

vi.mock("../../api/audit", () => ({
  auditAPI: {
    list: (...args: unknown[]) => mockList(...args),
    download: (...args: unknown[]) => mockDownload(...args),
  },
}));

import { auditAPI } from "../../api/audit";

/* ── Helpers ──────────────────────────────────────────────────────── */

function renderPage(initialPath = "/audit"): void {
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <AuditPage />
    </MemoryRouter>,
  );
}

const mockEntries = [
  {
    id: "1",
    timestamp: "2026-06-20T10:00:00Z",
    event_type: "hook.call",
    source: "harness",
    message: "Hook invoked",
  },
  {
    id: "2",
    timestamp: "2026-06-20T11:00:00Z",
    event_type: "plugin.enable",
    source: "admin",
    message: "Plugin enabled",
  },
];

/* ── Tests ────────────────────────────────────────────────────────── */

describe("AuditPage", () => {
  let createObjectURL: ReturnType<typeof vi.fn>;
  let revokeObjectURL: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({
      items: mockEntries,
      total: 2,
      offset: 0,
      limit: 20,
    });

    // Stable URL mocks
    createObjectURL = vi.fn(() => "blob:test");
    revokeObjectURL = vi.fn();
    globalThis.URL.createObjectURL = createObjectURL;
    globalThis.URL.revokeObjectURL = revokeObjectURL;
  });

  /* ────────────────────────────────────────────────────────────────── */

  it("renders date range picker + format selector", async () => {
    renderPage();

    // Date inputs should be present
    expect(screen.getByTestId("date-from")).toBeInTheDocument();
    expect(screen.getByTestId("date-to")).toBeInTheDocument();

    // Format selector radios
    expect(screen.getByTestId("format-json")).toBeInTheDocument();
    expect(screen.getByTestId("format-csv")).toBeInTheDocument();

    // Wait for table to load
    await waitFor(() => {
      expect(screen.getByText("hook.call")).toBeInTheDocument();
    });
  });

  /* ────────────────────────────────────────────────────────────────── */

  it("passes date filter in API call", async () => {
    const user = userEvent.setup();
    renderPage("/audit?from=2026-06-01&to=2026-06-30");

    // API should be called with the date params
    await waitFor(() => {
      expect(mockList).toHaveBeenCalledWith(
        expect.objectContaining({ from: "2026-06-01", to: "2026-06-30" }),
      );
    });

    // Change date
    const fromInput = screen.getByTestId("date-from");
    await user.clear(fromInput);
    await user.type(fromInput, "2026-06-15");

    // Should trigger re-fetch with new from date
    await waitFor(() => {
      expect(mockList).toHaveBeenCalledWith(
        expect.objectContaining({ from: "2026-06-15" }),
      );
    });
  });

  /* ────────────────────────────────────────────────────────────────── */

  it("triggers download API call with correct params", async () => {
    const blob = new Blob(["test,csv,data"], { type: "text/csv" });
    mockDownload.mockResolvedValue(blob);

    const user = userEvent.setup();
    renderPage("/audit?format=csv&from=2026-06-01&to=2026-06-20");

    // CSV radio should be checked
    await waitFor(() => {
      expect(screen.getByTestId("format-csv")).toBeChecked();
    });

    const downloadBtn = screen.getByTestId("download-btn");
    await user.click(downloadBtn);

    await waitFor(() => {
      // Verify download API was called with correct params
      expect(mockDownload).toHaveBeenCalledWith(
        expect.objectContaining({
          from: "2026-06-01",
          to: "2026-06-20",
          format: "csv",
        }),
      );
      // Verify blob URL was created (for the save)
      expect(createObjectURL).toHaveBeenCalledWith(blob);
    });

    // Verify revoke was called (after download trigger)
    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:test");
    });
  });

  /* ────────────────────────────────────────────────────────────────── */

  it("displays error state on API failure", async () => {
    mockList.mockRejectedValue(new Error("Server Error"));

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId("audit-error")).toBeInTheDocument();
      expect(screen.getByText("Server Error")).toBeInTheDocument();
    });
  });
});
