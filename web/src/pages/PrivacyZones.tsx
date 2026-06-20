/**
 * Phase 5.3 v1.25.0 — Privacy Zones admin page.
 *
 * Table listing all privacy zones with create / edit / delete actions.
 * Uses the PrivacyApiClient from ``../api/privacy`` and the ZoneModal
 * component from ``../components/ZoneModal``.
 *
 * Framework-agnostic JSX. Replace inline styles with your CSS framework
 * (Tailwind / Shadcn) in production.
 */

import { useState, useEffect, useCallback } from "react";
import {
  createPrivacyApiClient,
  type PrivacyZone,
  type ZoneAction,
} from "../api/privacy";
import { ZoneModal } from "../components/ZoneModal";

export function PrivacyZonesPage(): JSX.Element {
  const [zones, setZones] = useState<PrivacyZone[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingZone, setEditingZone] = useState<PrivacyZone | null>(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const client = createPrivacyApiClient(
    "/api/v1/privacy",
    () => localStorage.getItem("harness_token"),
  );

  const fetchZones = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const resp = await client.list();
      setZones(resp.zones);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load zones");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchZones();
  }, [fetchZones]);

  const handleCreate = (): void => {
    setEditingZone(null);
    setModalOpen(true);
  };

  const handleEdit = (zone: PrivacyZone): void => {
    setEditingZone(zone);
    setModalOpen(true);
  };

  const handleDelete = async (zoneId: string): Promise<void> => {
    try {
      await client.remove(zoneId);
      setDeleteConfirmId(null);
      await fetchZones();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  };

  const handleModalSubmit = async (values: {
    pattern: string;
    action: ZoneAction;
    description: string;
    enabled: boolean;
  }): Promise<void> => {
    if (editingZone) {
      await client.update(editingZone.id, {
        pattern: values.pattern,
        action: values.action,
        description: values.description || null,
        enabled: values.enabled,
      });
    } else {
      await client.create({
        pattern: values.pattern,
        action: values.action,
        description: values.description || undefined,
        enabled: values.enabled,
      });
    }
    setModalOpen(false);
    await fetchZones();
  };

  const formatDate = (iso: string): string => {
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  };

  const actionBadgeStyle = (action: ZoneAction): React.CSSProperties => {
    const colors: Record<ZoneAction, string> = {
      block: "#c62828",
      redact: "#f57c00",
      skip: "#757575",
    };
    return {
      display: "inline-block",
      padding: "2px 8px",
      borderRadius: "12px",
      color: "#fff",
      fontSize: "12px",
      fontWeight: 600,
      background: colors[action],
    };
  };

  return (
    <div style={{ maxWidth: "900px", margin: "0 auto", padding: "24px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
        <h1 style={{ margin: 0 }}>Privacy Zones</h1>
        <button
          onClick={handleCreate}
          style={{
            padding: "8px 16px",
            background: "#1976d2",
            color: "#fff",
            border: "none",
            borderRadius: "4px",
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          + New Zone
        </button>
      </div>

      {error && (
        <div
          style={{
            color: "#c62828",
            background: "#ffebee",
            padding: "12px",
            borderRadius: "4px",
            marginBottom: "16px",
          }}
        >
          {error}
        </div>
      )}

      {loading ? (
        <p style={{ color: "#666" }}>Loading...</p>
      ) : zones.length === 0 ? (
        <p style={{ color: "#666", textAlign: "center", padding: "40px" }}>
          No privacy zones configured. Click "New Zone" to create one.
        </p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #e0e0e0", textAlign: "left" }}>
              <th style={{ padding: "8px" }}>Pattern</th>
              <th style={{ padding: "8px" }}>Action</th>
              <th style={{ padding: "8px" }}>Enabled</th>
              <th style={{ padding: "8px" }}>Description</th>
              <th style={{ padding: "8px" }}>Updated</th>
              <th style={{ padding: "8px", textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {zones.map((zone) => (
              <tr
                key={zone.id}
                style={{ borderBottom: "1px solid #f0f0f0" }}
              >
                <td style={{ padding: "8px", fontFamily: "monospace" }}>
                  {zone.pattern}
                </td>
                <td style={{ padding: "8px" }}>
                  <span style={actionBadgeStyle(zone.action)}>
                    {zone.action}
                  </span>
                </td>
                <td style={{ padding: "8px" }}>
                  {zone.enabled ? (
                    <span style={{ color: "#2e7d32" }}>●</span>
                  ) : (
                    <span style={{ color: "#bdbdbd" }}>○</span>
                  )}
                </td>
                <td style={{ padding: "8px", color: "#666", maxWidth: "200px", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {zone.description ?? "—"}
                </td>
                <td style={{ padding: "8px", color: "#999", fontSize: "12px" }}>
                  {formatDate(zone.updated_at)}
                </td>
                <td style={{ padding: "8px", textAlign: "right" }}>
                  <button
                    onClick={() => handleEdit(zone)}
                    style={{
                      padding: "4px 8px",
                      border: "1px solid #ccc",
                      borderRadius: "3px",
                      background: "#fff",
                      cursor: "pointer",
                      marginRight: "4px",
                    }}
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => setDeleteConfirmId(zone.id)}
                    style={{
                      padding: "4px 8px",
                      border: "1px solid #ef5350",
                      borderRadius: "3px",
                      background: "#fff",
                      color: "#c62828",
                      cursor: "pointer",
                    }}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {modalOpen && (
        <ZoneModal
          zone={editingZone}
          onSubmit={handleModalSubmit}
          onClose={() => setModalOpen(false)}
        />
      )}

      {deleteConfirmId && (
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
        >
          <div
            style={{
              background: "#fff",
              borderRadius: "8px",
              padding: "24px",
              maxWidth: "360px",
            }}
          >
            <p style={{ marginTop: 0 }}>Delete this privacy zone?</p>
            <p style={{ color: "#666", fontSize: "14px" }}>
              This action cannot be undone.
            </p>
            <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
              <button
                onClick={() => setDeleteConfirmId(null)}
                style={{
                  padding: "8px 16px",
                  border: "1px solid #ccc",
                  borderRadius: "4px",
                  background: "#f5f5f5",
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                onClick={() => void handleDelete(deleteConfirmId)}
                style={{
                  padding: "8px 16px",
                  border: "none",
                  borderRadius: "4px",
                  background: "#c62828",
                  color: "#fff",
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default PrivacyZonesPage;
