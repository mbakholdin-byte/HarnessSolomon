/**
 * WI-02: Observability API.
 *
 * Backend: ``/api/v1/observability`` (Phase 4.11 Task B v1.21.0).
 * All endpoints require ``observability.read`` scope.
 *
 * Endpoints:
 *   GET /api/v1/observability/metrics?filter=    — JSON metrics snapshot
 *   GET /api/v1/observability/health/deep         — deep health report
 *   GET /api/v1/observability/audit/recent?limit=  — recent audit entries
 */

import { api } from "./client";

/** JSON snapshot of a single Prometheus metric series. */
export interface MetricSeries {
  name: string;
  type: "counter" | "gauge";
  help: string;
  value: number;
  labels?: Record<string, string>;
}

/** Top-level metrics response — metric name → series data. */
export type SystemMetrics = Record<string, MetricSeries>;

/** A single probe result in the deep health report. */
export interface HealthProbe {
  name: string;
  status: "healthy" | "unhealthy";
  latency_ms: number;
  error?: string;
}

/** Deep health report (8 subsystem probes). */
export interface HealthStatus {
  status: "healthy" | "unhealthy";
  version: string;
  project_root: string;
  checks: number;
  probes: HealthProbe[];
  ts: string;
}

/** A single entry from the hook audit log (PII stripped). */
export interface AuditEntry {
  ts: string;
  event: string;
  session_id: string;
  agent_id: string;
  request_id: string;
  aggregate: Record<string, unknown>;
}

const BASE = "/observability";

export const observAPI = {
  /** JSON snapshot of all Prometheus counters + gauges.
   *  Optional ``filter`` narrows returned metric names (regex). */
  getMetrics(filter?: string): Promise<SystemMetrics> {
    const qs = filter ? `?filter=${encodeURIComponent(filter)}` : "";
    return api.get<SystemMetrics>(`${BASE}/metrics${qs}`);
  },

  /** Deep health report — 8 subsystem probes, full timeouts. */
  getHealth(): Promise<HealthStatus> {
    return api.get<HealthStatus>(`${BASE}/health/deep`);
  },

  /** Last N audit entries (default 50, max 500).
   *  PII-bearing keys are stripped by the backend. */
  getAuditLog(limit: number = 50): Promise<AuditEntry[]> {
    return api.get<AuditEntry[]>(`${BASE}/audit/recent?limit=${limit}`);
  },
};
