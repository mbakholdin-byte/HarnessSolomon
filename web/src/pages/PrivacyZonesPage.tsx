/**
 * WI-04: Privacy Zones page — full admin interface.
 *
 * Loads zones via ``privacyZonesAPI.list()``, displays them in a shared
 * ``Table`` component, and supports create/edit via ``Modal`` and delete
 * via ``ConfirmDialog``. Uses CSS Modules for styling.
 */

import { useState, useEffect, useCallback } from "react";
import {
  type PrivacyZone,
  type ZoneAction,
  type PrivacyZoneCreate,
  type PrivacyZoneUpdate,
  privacyZonesAPI,
} from "../api/privacy";
import { APIError } from "../api/types";
import { Table, Modal, Badge, ConfirmDialog } from "../components";
import type { TableColumn } from "../components";
import type { BadgeVariant } from "../components";
import styles from "./PrivacyZonesPage.module.css";

/* ── Action → Badge mapping ──────────────────────────────────────── */

const ACTION_VARIANT: Record<ZoneAction, BadgeVariant> = {
  block: "error",
  redact: "warning",
  skip: "info",
};

const ACTION_OPTIONS: ZoneAction[] = ["block", "redact", "skip"];

/* ── Helpers ─────────────────────────────────────────────────────── */

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

/* ── Component ───────────────────────────────────────────────────── */

export function PrivacyZonesPage(): JSX.Element {
  const [zones, setZones] = useState<PrivacyZone[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Modal state
  const [modalOpen, setModalOpen] = useState(false);
  const [editingZone, setEditingZone] = useState<PrivacyZone | null>(null);

  // Delete confirm
  const [deleteTarget, setDeleteTarget] = useState<PrivacyZone | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Form state
  const [formName, setFormName] = useState("");
  const [formPatterns, setFormPatterns] = useState("");
  const [formAction, setFormAction] = useState<ZoneAction>("block");
  const [formEnabled, setFormEnabled] = useState(true);
  const [formError, setFormError] = useState<string | null>(null);
  const [formSubmitting, setFormSubmitting] = useState(false);

  /* ── Data fetching ─────────────────────────────────────────────── */

  const fetchZones = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const resp = await privacyZonesAPI.list();
      setZones(resp.zones);
    } catch (err) {
      setError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load zones",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchZones();
  }, [fetchZones]);

  /* ── Modal open / close ────────────────────────────────────────── */

  const openCreate = (): void => {
    setEditingZone(null);
    setFormName("");
    setFormPatterns("");
    setFormAction("block");
    setFormEnabled(true);
    setFormError(null);
    setModalOpen(true);
  };

  const openEdit = (zone: PrivacyZone): void => {
    setEditingZone(zone);
    setFormName(zone.pattern);
    setFormPatterns(zone.description ?? "");
    setFormAction(zone.action);
    setFormEnabled(zone.enabled);
    setFormError(null);
    setModalOpen(true);
  };

  const closeModal = (): void => {
    if (formSubmitting) return;
    setModalOpen(false);
  };

  /* ── Form submit ───────────────────────────────────────────────── */

  const handleFormSubmit = async (): Promise<void> => {
    if (!formName.trim()) {
      setFormError("Name (pattern) is required");
      return;
    }
    setFormSubmitting(true);
    setFormError(null);
    try {
      if (editingZone) {
        const body: PrivacyZoneUpdate = {
          pattern: formName.trim(),
          action: formAction,
          description: formPatterns.trim() || undefined,
          enabled: formEnabled,
        };
        await privacyZonesAPI.update(editingZone.id, body);
      } else {
        const body: PrivacyZoneCreate = {
          pattern: formName.trim(),
          action: formAction,
          description: formPatterns.trim() || undefined,
          enabled: formEnabled,
        };
        await privacyZonesAPI.create(body);
      }
      setModalOpen(false);
      await fetchZones();
    } catch (err) {
      setFormError(
        err instanceof Error ? err.message : "Submission failed",
      );
    } finally {
      setFormSubmitting(false);
    }
  };

  /* ── Delete ────────────────────────────────────────────────────── */

  const confirmDelete = (zone: PrivacyZone): void => {
    setDeleteTarget(zone);
    setDeleteError(null);
  };

  const handleDelete = async (): Promise<void> => {
    if (!deleteTarget) return;
    try {
      await privacyZonesAPI.delete(deleteTarget.id);
      setDeleteTarget(null);
      setDeleteError(null);
      await fetchZones();
    } catch (err) {
      setDeleteError(
        err instanceof Error ? err.message : "Delete failed",
      );
    }
  };

  /* ── Table columns ─────────────────────────────────────────────── */

  const columns: TableColumn<PrivacyZone>[] = [
    {
      key: "name",
      header: "Name",
      render: (row: PrivacyZone) => (
        <span style={{ fontFamily: "monospace", fontSize: "13px" }}>
          {row.pattern}
        </span>
      ),
    },
    {
      key: "patterns",
      header: "Patterns",
      render: (row: PrivacyZone) => (
        <span>{row.description ? row.description : "1 pattern"}</span>
      ),
    },
    {
      key: "action",
      header: "Action",
      render: (row: PrivacyZone) => (
        <Badge variant={ACTION_VARIANT[row.action]}>{row.action}</Badge>
      ),
      sortable: true,
    },
    {
      key: "enabled",
      header: "Enabled",
      render: (row: PrivacyZone) => (
        <Badge variant={row.enabled ? "success" : "info"}>
          {row.enabled ? "Yes" : "No"}
        </Badge>
      ),
      sortable: true,
    },
    {
      key: "created_at",
      header: "Created",
      render: (row: PrivacyZone) => formatDate(row.created_at),
      sortable: true,
    },
    {
      key: "actions",
      header: "",
      render: (row: PrivacyZone) => (
        <div className={styles.actions}>
          <button
            className={styles.editBtn}
            onClick={() => openEdit(row)}
            data-testid={`edit-zone-${row.id}`}
          >
            Edit
          </button>
          <button
            className={styles.deleteBtn}
            onClick={() => confirmDelete(row)}
            data-testid={`delete-zone-${row.id}`}
          >
            Delete
          </button>
        </div>
      ),
      sortable: false,
    },
  ];

  /* ── Render ────────────────────────────────────────────────────── */

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Privacy Zones</h1>
        <button
          className={styles.addBtn}
          onClick={openCreate}
          data-testid="add-zone-btn"
        >
          + Add Zone
        </button>
      </div>

      {error && <div className={styles.errorBlock}>{error}</div>}

      {loading ? (
        <div className={styles.loading}>Loading privacy zones...</div>
      ) : zones.length === 0 ? (
        <div className={styles.empty}>
          No privacy zones configured. Click "Add Zone" to create one.
        </div>
      ) : (
        <Table
          columns={columns}
          data={zones}
          defaultSortKey="created_at"
          defaultSortDirection="desc"
        />
      )}

      {/* ── Create / Edit Modal ──────────────────────────────────── */}
      <Modal
        open={modalOpen}
        onClose={closeModal}
        title={editingZone ? "Edit Privacy Zone" : "Create Privacy Zone"}
      >
        <div className={styles.formGroup}>
          <label className={styles.formLabel}>Name *</label>
          <input
            className={styles.formInput}
            type="text"
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            placeholder="private/* or **/.env"
            data-testid="zone-form-name"
          />
        </div>

        <div className={styles.formGroup}>
          <label className={styles.formLabel}>Patterns</label>
          <textarea
            className={styles.formTextarea}
            value={formPatterns}
            onChange={(e) => setFormPatterns(e.target.value)}
            placeholder="Optional pattern description or additional info"
            rows={3}
            data-testid="zone-form-patterns"
          />
        </div>

        <div className={styles.formGroup}>
          <label className={styles.formLabel}>Action</label>
          <select
            className={styles.formSelect}
            value={formAction}
            onChange={(e) => setFormAction(e.target.value as ZoneAction)}
            data-testid="zone-form-action"
          >
            {ACTION_OPTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </div>

        <label className={styles.formCheckbox}>
          <input
            type="checkbox"
            checked={formEnabled}
            onChange={(e) => setFormEnabled(e.target.checked)}
            data-testid="zone-form-enabled"
          />
          Enabled
        </label>

        {formError && (
          <div className={styles.errorBlock} style={{ marginBottom: "12px" }}>
            {formError}
          </div>
        )}

        <div
          style={{
            display: "flex",
            gap: "8px",
            justifyContent: "flex-end",
            marginTop: "8px",
          }}
        >
          <button
            onClick={closeModal}
            disabled={formSubmitting}
            className={styles.editBtn}
            style={{
              padding: "8px 16px",
              background: "#f5f5f5",
              borderColor: "#ccc",
              fontWeight: 600,
            }}
          >
            Cancel
          </button>
          <button
            onClick={() => void handleFormSubmit()}
            disabled={formSubmitting}
            style={{
              padding: "8px 16px",
              background: formSubmitting ? "#90caf9" : "#1976d2",
              color: "#fff",
              border: "none",
              borderRadius: "4px",
              fontWeight: 600,
              cursor: formSubmitting ? "not-allowed" : "pointer",
              fontSize: "14px",
            }}
            data-testid="zone-form-submit"
          >
            {formSubmitting
              ? "Saving..."
              : editingZone
                ? "Update"
                : "Create"}
          </button>
        </div>
      </Modal>

      {/* ── Delete ConfirmDialog ──────────────────────────────────── */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete Privacy Zone"
        message={
          deleteTarget
            ? `Delete zone "${deleteTarget.pattern}"? This action cannot be undone.`
            : "Delete this zone?"
        }
        confirmLabel="Delete"
        cancelLabel="Cancel"
        onConfirm={() => void handleDelete()}
        onCancel={() => {
          setDeleteTarget(null);
          setDeleteError(null);
        }}
      />

      {/* ── Delete error (shown globally, not inside dialog) ──────── */}
      {deleteError && (
        <div className={styles.errorBlock} style={{ marginTop: "8px" }}>
          {deleteError}
        </div>
      )}
    </div>
  );
}

export default PrivacyZonesPage;
