/**
 * Phase 5.3 v1.25.0 — ZoneModal component.
 *
 * Modal form for creating or editing a privacy zone. Pure presentational
 * component — all state is managed by the parent. Calls ``onSubmit`` with
 * the form values and ``onClose`` to dismiss.
 *
 * Framework-agnostic JSX (works with Next.js / CRA / Vite). No CSS
 * framework dependency — uses inline styles for portability. Replace
 * with Shadcn/UI components in production.
 */

import { useState, useEffect, type FormEvent } from "react";
import type { PrivacyZone, ZoneAction } from "../api/privacy";

export interface ZoneModalProps {
  /** Existing zone for edit mode, ``null`` for create mode. */
  zone: PrivacyZone | null;
  /** Submit handler. Receives form values. */
  onSubmit: (values: {
    pattern: string;
    action: ZoneAction;
    description: string;
    enabled: boolean;
  }) => Promise<void>;
  /** Close handler. */
  onClose: () => void;
}

const ACTION_OPTIONS: ZoneAction[] = ["block", "redact", "skip"];

export function ZoneModal({ zone, onSubmit, onClose }: ZoneModalProps): JSX.Element {
  const isEdit = zone !== null;
  const [pattern, setPattern] = useState(zone?.pattern ?? "");
  const [action, setAction] = useState<ZoneAction>(zone?.action ?? "block");
  const [description, setDescription] = useState(zone?.description ?? "");
  const [enabled, setEnabled] = useState(zone?.enabled ?? true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset form when the ``zone`` prop changes (e.g. opening the modal
  // for a different zone without unmounting).
  useEffect(() => {
    setPattern(zone?.pattern ?? "");
    setAction(zone?.action ?? "block");
    setDescription(zone?.description ?? "");
    setEnabled(zone?.enabled ?? true);
    setError(null);
  }, [zone]);

  const handleSubmit = async (e: FormEvent): Promise<void> => {
    e.preventDefault();
    if (!pattern.trim()) {
      setError("Pattern is required");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit({
        pattern: pattern.trim(),
        action,
        description: description.trim(),
        enabled,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Submission failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: "#fff",
          borderRadius: "8px",
          padding: "24px",
          maxWidth: "480px",
          width: "90%",
          maxHeight: "90vh",
          overflowY: "auto",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 style={{ marginTop: 0 }}>
          {isEdit ? "Edit Privacy Zone" : "Create Privacy Zone"}
        </h2>
        <form onSubmit={handleSubmit}>
          <label style={{ display: "block", marginBottom: "12px" }}>
            <span style={{ display: "block", fontWeight: 600, marginBottom: "4px" }}>
              Pattern *
            </span>
            <input
              type="text"
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
              placeholder="private/* or **/.env"
              style={{ width: "100%", padding: "8px", boxSizing: "border-box" }}
              required
              autoFocus
            />
          </label>

          <label style={{ display: "block", marginBottom: "12px" }}>
            <span style={{ display: "block", fontWeight: 600, marginBottom: "4px" }}>
              Action
            </span>
            <select
              value={action}
              onChange={(e) => setAction(e.target.value as ZoneAction)}
              style={{ width: "100%", padding: "8px", boxSizing: "border-box" }}
            >
              {ACTION_OPTIONS.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>

          <label style={{ display: "block", marginBottom: "12px" }}>
            <span style={{ display: "block", fontWeight: 600, marginBottom: "4px" }}>
              Description
            </span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Optional human-readable description"
              rows={2}
              style={{ width: "100%", padding: "8px", boxSizing: "border-box" }}
            />
          </label>

          <label style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "16px" }}>
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span style={{ fontWeight: 600 }}>Enabled</span>
          </label>

          {error && (
            <div
              style={{
                color: "#c62828",
                background: "#ffebee",
                padding: "8px 12px",
                borderRadius: "4px",
                marginBottom: "12px",
                fontSize: "14px",
              }}
            >
              {error}
            </div>
          )}

          <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              style={{
                padding: "8px 16px",
                border: "1px solid #ccc",
                borderRadius: "4px",
                background: "#f5f5f5",
                cursor: submitting ? "not-allowed" : "pointer",
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              style={{
                padding: "8px 16px",
                border: "none",
                borderRadius: "4px",
                background: submitting ? "#90caf9" : "#1976d2",
                color: "#fff",
                fontWeight: 600,
                cursor: submitting ? "not-allowed" : "pointer",
              }}
            >
              {submitting ? "Saving..." : isEdit ? "Update" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
