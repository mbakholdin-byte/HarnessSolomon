/**
 * WI-04: HooksPage tests.
 *
 * Tests that the page renders the hook event list and handles
 * loading/error states.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { HooksPage } from "../HooksPage";

vi.mock("../../api/hooks", () => ({
  hooksAPI: {
    listEvents: vi.fn(),
    getConfig: vi.fn(),
    updateConfig: vi.fn(),
    enable: vi.fn(),
    disable: vi.fn(),
  },
}));

import { hooksAPI } from "../../api/hooks";

const mockEvents = [
  {
    name: "SessionStart",
    description: "Fired when a new session begins",
    callback_count: 3,
  },
  {
    name: "PreToolUse",
    description: "Fired before a tool is executed",
    callback_count: 5,
  },
  {
    name: "PostToolUse",
    description: "Fired after a tool is executed",
    callback_count: 2,
  },
];

describe("HooksPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders event list", async () => {
    vi.mocked(hooksAPI.listEvents).mockResolvedValue(mockEvents);

    render(<HooksPage />);

    await waitFor(() => {
      expect(screen.getByText("SessionStart")).toBeInTheDocument();
    });

    expect(screen.getByText("PreToolUse")).toBeInTheDocument();
    expect(screen.getByText("PostToolUse")).toBeInTheDocument();
    expect(screen.getByText("Fired when a new session begins")).toBeInTheDocument();
  });

  it("shows loading state", () => {
    vi.mocked(hooksAPI.listEvents).mockReturnValue(
      new Promise(() => {
        /* never resolves */
      }),
    );

    render(<HooksPage />);
    expect(screen.getByText("Loading hook events...")).toBeInTheDocument();
  });

  it("shows error on API failure", async () => {
    vi.mocked(hooksAPI.listEvents).mockRejectedValue(
      new Error("Server Error"),
    );

    render(<HooksPage />);

    await waitFor(() => {
      expect(screen.getByText("Server Error")).toBeInTheDocument();
    });
  });
});
