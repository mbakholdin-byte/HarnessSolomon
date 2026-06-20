/**
 * WI-02: Hooks API.
 *
 * Backend hooks exist (Phase 4.4+ v1.14.0) — 12 lifecycle events:
 * SessionStart, SessionEnd, PreToolUse, PostToolUse, etc. The REST admin
 * surface is planned for a later phase; frontend module matches the
 * anticipated API shape.
 *
 * Endpoints (planned):
 *   GET    /api/v1/hooks/events        — list registered hook events
 *   GET    /api/v1/hooks/config        — current hook config
 *   PATCH  /api/v1/hooks/config        — update config
 *   POST   /api/v1/hooks/enable        — enable hooks system
 *   POST   /api/v1/hooks/disable       — disable hooks system
 */

import { api } from "./client";

/** A lifecycle event emitted by the Harness hook system. */
export interface HookEvent {
  name: string;
  description: string;
  /** Number of registered callbacks for this event. */
  callback_count: number;
}

/** Runtime configuration of the hook subsystem. */
export interface HookConfig {
  enabled: boolean;
  audit_log: boolean;
  elicitation_enabled: boolean;
  elicitation_longpoll_enabled: boolean;
  elicitation_sse_enabled: boolean;
  /** Max callback execution time in milliseconds. */
  default_timeout_ms: number;
}

const BASE = "/hooks";

export const hooksAPI = {
  /** List all registered hook events with callback counts. */
  listEvents(): Promise<HookEvent[]> {
    return api.get<HookEvent[]>(`${BASE}/events`);
  },

  /** Get current hook subsystem configuration. */
  getConfig(): Promise<HookConfig> {
    return api.get<HookConfig>(`${BASE}/config`);
  },

  /** Update hook configuration (partial — only provided fields). */
  updateConfig(patch: Partial<HookConfig>): Promise<HookConfig> {
    return api.patch<HookConfig>(`${BASE}/config`, patch);
  },

  /** Enable the hook subsystem. Idempotent. */
  enable(): Promise<{ enabled: boolean }> {
    return api.post<{ enabled: boolean }>(`${BASE}/enable`);
  },

  /** Disable the hook subsystem. Idempotent. */
  disable(): Promise<{ enabled: boolean }> {
    return api.post<{ enabled: boolean }>(`${BASE}/disable`);
  },
};
