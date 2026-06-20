/**
 * WI-05: AuditPage — admin audit log with date filtering, format selector,
 * table display, blob download, and pagination.
 *
 * URL query params (synced via useSearchParams):
 *   from, to, format, limit, offset
 */

import { useState, useEffect, useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { auditAPI, type AuditEntry, type AuditParams } from "../api/audit";
import { APIError } from "../api/types";
import type { Page } from "../api/types";
import { Table, DateRangePicker } from "../components";
import type { TableColumn } from "../components";
import styles from "./AuditPage.module.css";

/* ── Constants ────────────────────────────────────────────────────── */

const DEFAULT_LIMIT = 20;
const FORMAT_OPTIONS = [
  { value: "json", label: "JSON" },
  { value: "csv", label: "CSV" },
] as const;

/* ── Helpers ──────────────────────────────────────────────────────── */

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/* ── Component ────────────────────────────────────────────────────── */

export function AuditPage(): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();

  /* ── URL-derived state ──────────────────────────────────────────── */
  const from = searchParams.get("from") || "";
  const to = searchParams.get("to") || "";
  const format = (searchParams.get("format") as "json" | "csv") || "json";
  const limit = Number(searchParams.get("limit")) || DEFAULT_LIMIT;
  const offset = Number(searchParams.get("offset")) || 0;

  /* ── Data state ─────────────────────────────────────────────────── */
  const [page, setPage] = useState<Page<AuditEntry> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  /* ── Update URL param helper ────────────────────────────────────── */
  const setParam = useCallback(
    (key: string, value: string) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        if (value) {
          next.set(key, value);
        } else {
          next.delete(key);
        }
        return next;
      });
    },
    [setSearchParams],
  );

  /* ── Fetch audit log ────────────────────────────────────────────── */
  const fetchAudit = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const params: AuditParams = { limit, offset };
      if (from) params.from = from;
      if (to) params.to = to;
      const data = await auditAPI.list(params);
      setPage(data);
    } catch (err) {
      setError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load audit log",
      );
    } finally {
      setLoading(false);
    }
  }, [from, to, limit, offset]);

  useEffect(() => {
    void fetchAudit();
  }, [fetchAudit]);

  /* ── Format selector ────────────────────────────────────────────── */
  const handleFormatChange = (value: "json" | "csv"): void => {
    setParam("format", value);
  };

  /* ── Download ───────────────────────────────────────────────────── */
  const handleDownload = async (): Promise<void> => {
    setDownloading(true);
    setError(null);
    try {
      const blob = await auditAPI.download({ from, to, format });
      const ext = format === "csv" ? "csv" : "json";
      const filename = `audit_${new Date().toISOString().slice(0, 10)}.${ext}`;
      triggerBlobDownload(blob, filename);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Download failed",
      );
    } finally {
      setDownloading(false);
    }
  };

  /* ── Date range handler ─────────────────────────────────────────── */
  const handleDateChange = (newFrom: string, newTo: string): void => {
    // Update both params atomically, reset offset
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (newFrom) next.set("from", newFrom);
      else next.delete("from");
      if (newTo) next.set("to", newTo);
      else next.delete("to");
      next.delete("offset");
      return next;
    });
  };

  /* ── Pagination ─────────────────────────────────────────────────── */
  const totalPages = page ? Math.ceil(page.total / page.limit) : 0;
  const currentPage = Math.floor(offset / limit) + 1;
  const hasPrev = offset > 0;
  const hasNext = page ? offset + limit < page.total : false;

  const goToPage = (newOffset: number): void => {
    setParam("offset", String(newOffset));
  };

  /* ── Table columns ──────────────────────────────────────────────── */
  const columns: TableColumn<AuditEntry>[] = useMemo(
    () => [
      {
        key: "timestamp",
        header: "Timestamp",
        render: (row: AuditEntry) => (
          <span className={styles.timestampCell}>
            {formatTimestamp(row.timestamp)}
          </span>
        ),
        sortable: true,
      },
      {
        key: "event_type",
        header: "Event Type",
        sortable: true,
      },
      {
        key: "source",
        header: "Source",
        sortable: true,
      },
      {
        key: "message",
        header: "Message",
        sortable: false,
      },
    ],
    [],
  );

  /* ── Render ─────────────────────────────────────────────────────── */
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Audit Log</h1>

      {/* ── Toolbar ────────────────────────────────────────────────── */}
      <div className={styles.toolbar}>
        <DateRangePicker from={from} to={to} onChange={handleDateChange} />

        <div className={styles.formatGroup}>
          {FORMAT_OPTIONS.map((opt) => (
            <label key={opt.value} className={styles.formatLabel}>
              <input
                type="radio"
                name="audit-format"
                value={opt.value}
                checked={format === opt.value}
                onChange={() => handleFormatChange(opt.value)}
                data-testid={`format-${opt.value}`}
              />
              {opt.label}
            </label>
          ))}
        </div>

        <button
          className={styles.downloadBtn}
          onClick={() => void handleDownload()}
          disabled={downloading}
          data-testid="download-btn"
        >
          {downloading ? "Downloading..." : `Download ${format.toUpperCase()}`}
        </button>
      </div>

      {/* ── Error ──────────────────────────────────────────────────── */}
      {error && (
        <div className={styles.errorBlock} data-testid="audit-error">
          {error}
        </div>
      )}

      {/* ── Table / Loading / Empty ────────────────────────────────── */}
      {loading ? (
        <div className={styles.loading}>Loading audit log...</div>
      ) : page && page.items.length === 0 ? (
        <div className={styles.empty}>No audit entries found.</div>
      ) : page ? (
        <>
          <Table
            columns={columns}
            data={page.items}
            defaultSortKey="timestamp"
            defaultSortDirection="desc"
          />

          {/* ── Pagination ─────────────────────────────────────────── */}
          <div className={styles.pagination}>
            <button
              className={styles.pageBtn}
              onClick={() => goToPage(offset - limit)}
              disabled={!hasPrev}
              data-testid="page-prev"
            >
              ← Prev
            </button>
            <span className={styles.pageInfo}>
              Page {currentPage} of {totalPages || 1} ({page.total} total)
            </span>
            <button
              className={styles.pageBtn}
              onClick={() => goToPage(offset + limit)}
              disabled={!hasNext}
              data-testid="page-next"
            >
              Next →
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

export default AuditPage;
