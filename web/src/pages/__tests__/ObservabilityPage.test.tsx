/**
 * WI-04: ObservabilityPage tests.
 *
 * Tests that the page renders the metrics tab with metric cards
 * and handles tab switching.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ObservabilityPage } from "../ObservabilityPage";

vi.mock("../../api/observability", () => ({
  observAPI: {
    getMetrics: vi.fn(),
    getHealth: vi.fn(),
    getAuditLog: vi.fn(),
  },
}));

import { observAPI } from "../../api/observability";

const mockMetrics = {
  total_events: {
    name: "total_events",
    type: "counter" as const,
    help: "Total hook events processed",
    value: 12345,
  },
  active_hooks: {
    name: "active_hooks",
    type: "gauge" as const,
    help: "Currently active hooks",
    value: 12,
  },
};

describe("ObservabilityPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders metrics tab with cards", async () => {
    vi.mocked(observAPI.getMetrics).mockResolvedValue(mockMetrics);

    render(<ObservabilityPage />);

    await waitFor(() => {
      expect(screen.getByText("12345")).toBeInTheDocument();
    });

    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("total_events")).toBeInTheDocument();
    expect(screen.getByText("active_hooks")).toBeInTheDocument();
  });

  it("shows loading state on metrics tab", () => {
    vi.mocked(observAPI.getMetrics).mockReturnValue(
      new Promise(() => {
        /* never resolves */
      }),
    );

    render(<ObservabilityPage />);
    expect(screen.getByText("Loading metrics...")).toBeInTheDocument();
  });

  it("switches to Health tab", async () => {
    vi.mocked(observAPI.getMetrics).mockResolvedValue(mockMetrics);
    vi.mocked(observAPI.getHealth).mockResolvedValue({
      status: "healthy",
      version: "1.0.0",
      project_root: "/app",
      checks: 8,
      probes: [
        {
          name: "database",
          status: "healthy",
          latency_ms: 12,
        },
      ],
      ts: "2026-06-20T00:00:00Z",
    });

    render(<ObservabilityPage />);

    fireEvent.click(screen.getByTestId("tab-health"));

    await waitFor(() => {
      expect(screen.getByText("HEALTHY")).toBeInTheDocument();
    });

    expect(screen.getByText("database")).toBeInTheDocument();
  });

  it("shows error on metrics failure", async () => {
    vi.mocked(observAPI.getMetrics).mockRejectedValue(
      new Error("Metrics unavailable"),
    );

    render(<ObservabilityPage />);

    await waitFor(() => {
      expect(screen.getByText("Metrics unavailable")).toBeInTheDocument();
    });
  });
});
