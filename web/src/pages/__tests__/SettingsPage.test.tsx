/**
 * WI-04: SettingsPage tests.
 *
 * Tests that the static settings page renders all sections correctly.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { SettingsPage } from "../SettingsPage";

describe("SettingsPage", () => {
  it("renders all sections", () => {
    render(<SettingsPage />);

    // Header
    expect(screen.getByText("Settings")).toBeInTheDocument();

    // Sections
    expect(screen.getByText("General")).toBeInTheDocument();
    expect(screen.getByText("API Keys")).toBeInTheDocument();
    expect(screen.getByText("About")).toBeInTheDocument();

    // Content
    expect(
      screen.getByText(/Configure general application settings/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Manage API tokens and access keys/),
    ).toBeInTheDocument();
  });

  it("renders version information", () => {
    render(<SettingsPage />);

    expect(screen.getByText("1.0.0")).toBeInTheDocument();
    expect(screen.getByText("2026-06-20")).toBeInTheDocument();
    expect(screen.getByText("React 18 + Vite + TypeScript")).toBeInTheDocument();
    expect(screen.getByText("MIT")).toBeInTheDocument();
  });

  it("renders placeholder messages", () => {
    render(<SettingsPage />);

    const placeholders = screen.getAllByText(/available in a future update/);
    // Should appear twice: for General and API Keys
    expect(placeholders.length).toBe(2);

    expect(placeholders[0]).toBeInTheDocument();
    expect(placeholders[1]).toBeInTheDocument();
  });
});
