/**
 * WI-02: Plugins API.
 *
 * Backend plugin system exists (Phase 6.2A v1.27.0 + 6.3 v1.28.0) —
 * plugins are loaded from ``settings.plugins_dir`` and dispatched via
 * ``PluginDispatcher``. The REST admin surface is planned for a later
 * phase; frontend module matches the anticipated API shape.
 *
 * Endpoints (planned):
 *   GET    /api/v1/plugins               — list loaded plugins
 *   POST   /api/v1/plugins/{name}/enable  — enable a plugin
 *   POST   /api/v1/plugins/{name}/disable — disable a plugin
 */

import { api } from "./client";

/** A loaded plugin visible to the admin dashboard. */
export interface Plugin {
  name: string;
  version: string;
  enabled: boolean;
  /** Hook events this plugin subscribes to. */
  hooks: string[];
}

const BASE = "/plugins";

export const pluginsAPI = {
  /** List all loaded plugins with their status. */
  list(): Promise<Plugin[]> {
    return api.get<Plugin[]>(BASE);
  },

  /** Enable a plugin by name. Idempotent. */
  enable(name: string): Promise<{ name: string; enabled: boolean }> {
    return api.post<{ name: string; enabled: boolean }>(
      `${BASE}/${encodeURIComponent(name)}/enable`,
    );
  },

  /** Disable a plugin by name. Idempotent. */
  disable(name: string): Promise<{ name: string; enabled: boolean }> {
    return api.post<{ name: string; enabled: boolean }>(
      `${BASE}/${encodeURIComponent(name)}/disable`,
    );
  },
};
