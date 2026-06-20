/**
 * WI-04: PluginsPage tests.
 *
 * Tests that the page renders the plugin list with status badges
 * and handles loading/error states.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { PluginsPage } from "../PluginsPage";

vi.mock("../../api/plugins", () => ({
  pluginsAPI: {
    list: vi.fn(),
    enable: vi.fn(),
    disable: vi.fn(),
  },
}));

import { pluginsAPI } from "../../api/plugins";

const mockPlugins = [
  {
    name: "memory-plugin",
    version: "1.2.0",
    enabled: true,
    hooks: ["SessionStart", "SessionEnd"],
  },
  {
    name: "logger-plugin",
    version: "0.9.0",
    enabled: false,
    hooks: [],
  },
  {
    name: "sanitizer",
    version: "2.0.1",
    enabled: true,
    hooks: ["PreToolUse", "PostToolUse"],
  },
];

describe("PluginsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders plugin list", async () => {
    vi.mocked(pluginsAPI.list).mockResolvedValue(mockPlugins);

    render(<PluginsPage />);

    await waitFor(() => {
      expect(screen.getByText("memory-plugin")).toBeInTheDocument();
    });

    expect(screen.getByText("logger-plugin")).toBeInTheDocument();
    expect(screen.getByText("sanitizer")).toBeInTheDocument();
    expect(screen.getByText("1.2.0")).toBeInTheDocument();
    // Two plugins (memory-plugin, sanitizer) have hook count 2
    expect(screen.getAllByText("2").length).toBe(2);
    expect(screen.getByText("0")).toBeInTheDocument(); // hooks count for logger
  });

  it("shows loading state", () => {
    vi.mocked(pluginsAPI.list).mockReturnValue(
      new Promise(() => {
        /* never resolves */
      }),
    );

    render(<PluginsPage />);
    expect(screen.getByText("Loading plugins...")).toBeInTheDocument();
  });

  it("shows error on API failure", async () => {
    vi.mocked(pluginsAPI.list).mockRejectedValue(
      new Error("Plugins unavailable"),
    );

    render(<PluginsPage />);

    await waitFor(() => {
      expect(screen.getByText("Plugins unavailable")).toBeInTheDocument();
    });
  });
});
