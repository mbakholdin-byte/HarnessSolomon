/**
 * WI-04: PrivacyZonesPage tests.
 *
 * Tests that the page renders a table with mock zone data
 * and shows loading/empty states correctly.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { PrivacyZonesPage } from "../PrivacyZonesPage";

vi.mock("../../api/privacy", () => ({
  privacyZonesAPI: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
}));

import { privacyZonesAPI } from "../../api/privacy";

const mockZones = [
  {
    id: "z1",
    pattern: "private/*",
    action: "block" as const,
    description: "Block private files",
    enabled: true,
    created_at: "2026-06-15T10:00:00Z",
    updated_at: "2026-06-15T10:00:00Z",
  },
  {
    id: "z2",
    pattern: "**/.env",
    action: "redact" as const,
    description: "Redact env files",
    enabled: false,
    created_at: "2026-06-16T12:00:00Z",
    updated_at: "2026-06-16T12:00:00Z",
  },
];

describe("PrivacyZonesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders table with zones", async () => {
    vi.mocked(privacyZonesAPI.list).mockResolvedValue({
      zones: mockZones,
      total: 2,
    });

    render(<PrivacyZonesPage />);

    await waitFor(() => {
      // "private/*" appears in the Name column
      expect(screen.getByText("private/*")).toBeInTheDocument();
    });

    expect(screen.getByText("**/.env")).toBeInTheDocument();
    expect(screen.getByText("block")).toBeInTheDocument();
    expect(screen.getByText("redact")).toBeInTheDocument();
  });

  it("shows loading state initially", () => {
    vi.mocked(privacyZonesAPI.list).mockReturnValue(
      new Promise(() => {
        /* never resolves */
      }),
    );

    render(<PrivacyZonesPage />);
    expect(screen.getByText("Loading privacy zones...")).toBeInTheDocument();
  });

  it("shows empty state when no zones", async () => {
    vi.mocked(privacyZonesAPI.list).mockResolvedValue({
      zones: [],
      total: 0,
    });

    render(<PrivacyZonesPage />);

    await waitFor(() => {
      expect(screen.getByText(/No privacy zones configured/)).toBeInTheDocument();
    });
  });

  it("shows error state on API failure", async () => {
    vi.mocked(privacyZonesAPI.list).mockRejectedValue(
      new Error("Network Error"),
    );

    render(<PrivacyZonesPage />);

    await waitFor(() => {
      expect(screen.getByText("Network Error")).toBeInTheDocument();
    });
  });
});
