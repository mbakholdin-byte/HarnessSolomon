/**
 * WI-05: Observability page — metrics, audit log, and health dashboard.
 *
 * Tab-based layout: Metrics | Audit Log | Health.
 *
 * WI-05: WebSocket integration — replaces polling for metrics + health.
 * Both metrics and health arrive in real-time via WS (topics: "metrics",
 * "health"), updating every ~1s. Audit tab remains REST-based.
 */

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  observAPI,
  type SystemMetrics,
  type AuditEntry,
  type HealthStatus,
} from "../api/observability";
import { APIError } from "../api/types";
import { ObservabilityWS } from "../api/ws";
import { Table, Badge } from "../components";
import type { TableColumn } from "../components";
import styles from "./ObservabilityPage.module.css";

/* ── Constants ────────────────────────────────────────────────────── */

const AUTH_TOKEN_KEY = "auth_token";

function getWsUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v1/observability/ws`;
}

/* ── Types ───────────────────────────────────────────────────────── */

type Tab = "metrics" | "audit" | "health";

/* ── Helpers ─────────────────────────────────────────────────────── */

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

/* ── Component ───────────────────────────────────────────────────── */

export function ObservabilityPage(): JSX.Element {
  const [activeTab, setActiveTab] = useState<Tab>("metrics");

  // Metrics
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [metricsError, setMetricsError] = useState<string | null>(null);

  // Audit log
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditTypeFilter, setAuditTypeFilter] = useState("");

  // Health
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [healthError, setHealthError] = useState<string | null>(null);

  // WebSocket ref
  const wsRef = useRef<ObservabilityWS | null>(null);

  /* ── Fetch data for active tab (initial one-time) ───────────────── */

  const fetchMetrics = useCallback(async (): Promise<void> => {
    setMetricsLoading(true);
    setMetricsError(null);
    try {
      const data = await observAPI.getMetrics();
      setMetrics(data);
    } catch (err) {
      setMetricsError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load metrics",
      );
    } finally {
      setMetricsLoading(false);
    }
  }, []);

  const fetchAuditLog = useCallback(async (): Promise<void> => {
    setAuditLoading(true);
    setAuditError(null);
    try {
      const data = await observAPI.getAuditLog(100);
      setAuditEntries(data);
    } catch (err) {
      setAuditError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load audit log",
      );
    } finally {
      setAuditLoading(false);
    }
  }, []);

  const fetchHealth = useCallback(async (): Promise<void> => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      const data = await observAPI.getHealth();
      setHealth(data);
    } catch (err) {
      setHealthError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load health status",
      );
    } finally {
      setHealthLoading(false);
    }
  }, []);

  useEffect(() => {
    switch (activeTab) {
      case "metrics":
        if (!metrics) void fetchMetrics();
        break;
      case "audit":
        if (auditEntries.length === 0) void fetchAuditLog();
        break;
      case "health":
        if (!health) void fetchHealth();
        break;
    }
  }, [activeTab, metrics, auditEntries.length, health, fetchMetrics, fetchAuditLog, fetchHealth]);

  /* ── WebSocket for real-time metrics + health ───────────────────── */

  useEffect(() => {
    const token = localStorage.getItem(AUTH_TOKEN_KEY) || "";
    const ws = new ObservabilityWS(getWsUrl(), token);
    wsRef.current = ws;

    ws.onMessage((msg: Record<string, unknown>) => {
      // Real-time metrics update
      if (msg.type === "metrics" && msg.data) {
        setMetrics(msg.data as SystemMetrics);
        setMetricsError(null);
      }

      // Real-time health probe update
      if (msg.type === "health" && msg.data) {
        setHealth(msg.data as HealthStatus);
        setHealthError(null);
      }
    });

    ws.connect()
      .then(() => {
        ws.subscribe(["metrics", "health"]);
      })
      .catch(() => {
        // WS connection failed — REST fallback works.
      });

    return () => {
      ws.disconnect();
    };
  }, []);

  /* ── Audit table columns ───────────────────────────────────────── */

  const auditColumns: TableColumn<AuditEntry>[] = [
    {
      key: "ts",
      header: "Time",
      render: (row: AuditEntry) => (
        <span style={{ fontSize: "12px" }}>{formatDate(row.ts)}</span>
      ),
      sortable: true,
    },
    {
      key: "event",
      header: "Event",
      sortable: true,
    },
    {
      key: "agent_id",
      header: "Agent",
      sortable: true,
    },
    {
      key: "session_id",
      header: "Session",
      render: (row: AuditEntry) => (
        <span style={{ fontFamily: "monospace", fontSize: "11px" }}>
          {row.session_id.slice(0, 12)}...
        </span>
      ),
      sortable: false,
    },
  ];

  /* ── Filtered audit data ───────────────────────────────────────── */

  const filteredAudit = useMemo(() => {
    if (!auditTypeFilter) return auditEntries;
    const lower = auditTypeFilter.toLowerCase();
    return auditEntries.filter((entry) =>
      entry.event.toLowerCase().includes(lower),
    );
  }, [auditEntries, auditTypeFilter]);

  /* ── Render ────────────────────────────────────────────────────── */

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Observability</h1>

      {/* Tab bar */}
      <div className={styles.tabs}>
        {(["metrics", "audit", "health"] as Tab[]).map((tab) => (
          <button
            key={tab}
            className={activeTab === tab ? styles.tabActive : styles.tab}
            onClick={() => setActiveTab(tab)}
            data-testid={`tab-${tab}`}
          >
            {tab === "metrics" ? "Metrics" : tab === "audit" ? "Audit Log" : "Health"}
          </button>
        ))}
      </div>

      {/* ── Metrics Tab ───────────────────────────────────────────── */}
      {activeTab === "metrics" && (
        <>
          {metricsError && (
            <div className={styles.errorBlock}>{metricsError}</div>
          )}
          {metricsLoading && !metrics ? (
            <div className={styles.loading}>Loading metrics...</div>
          ) : metrics ? (
            <div className={styles.metricsGrid}>
              {Object.entries(metrics).map(([name, series]) => (
                <div key={name} className={styles.metricCard}>
                  <p className={styles.metricValue}>{series.value}</p>
                  <p className={styles.metricLabel}>{name}</p>
                  {series.help && (
                    <p className={styles.metricHelp}>{series.help}</p>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className={styles.loading}>No metrics available</div>
          )}
        </>
      )}

      {/* ── Audit Log Tab ─────────────────────────────────────────── */}
      {activeTab === "audit" && (
        <>
          {auditError && (
            <div className={styles.errorBlock}>{auditError}</div>
          )}

          <div className={styles.filters}>
            <label>
              Event type:{" "}
              <input
                className={styles.filterInput}
                type="text"
                value={auditTypeFilter}
                onChange={(e) => setAuditTypeFilter(e.target.value)}
                placeholder="Filter by event..."
                data-testid="audit-filter"
              />
            </label>
          </div>

          {auditLoading ? (
            <div className={styles.loading}>Loading audit log...</div>
          ) : (
            <Table
              columns={auditColumns}
              data={filteredAudit}
              defaultSortKey="ts"
              defaultSortDirection="desc"
            />
          )}
        </>
      )}

      {/* ── Health Tab ────────────────────────────────────────────── */}
      {activeTab === "health" && (
        <>
          {healthError && (
            <div className={styles.errorBlock}>{healthError}</div>
          )}
          {healthLoading && !health ? (
            <div className={styles.loading}>Checking health...</div>
          ) : health ? (
            <div>
              <div style={{ marginBottom: "16px" }}>
                <Badge
                  variant={health.status === "healthy" ? "success" : "error"}
                >
                  {health.status.toUpperCase()}
                </Badge>
                <span
                  style={{
                    marginLeft: "12px",
                    fontSize: "13px",
                    color: "#666",
                  }}
                >
                  v{health.version} &middot; {health.checks} checks
                </span>
              </div>

              <ul className={styles.healthList}>
                {health.probes.map((probe) => (
                  <li key={probe.name} className={styles.healthItem}>
                    <span>
                      <span className={styles.healthName}>{probe.name}</span>
                      <span className={styles.healthLatency}>
                        {probe.latency_ms}ms
                      </span>
                    </span>
                    <span>
                      <Badge
                        variant={
                          probe.status === "healthy" ? "success" : "error"
                        }
                      >
                        {probe.status}
                      </Badge>
                      {probe.error && (
                        <span className={styles.healthError}>
                          {probe.error}
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

export default ObservabilityPage;
