/**
 * WI-05: Plugins page — manage loaded plugins.
 *
 * Lists all plugins with enable/disable toggles. Uses the Table and
 * Badge components from the shared library.
 *
 * WI-05: WebSocket integration — replaces polling for live plugin
 * enable/disable state. Initial data loaded via REST; subsequent
 * state changes arrive via WS (topic: "plugins").
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { pluginsAPI, type Plugin } from "../api/plugins";
import { APIError } from "../api/types";
import { ObservabilityWS } from "../api/ws";
import { Table, Badge } from "../components";
import type { TableColumn } from "../components";
import styles from "./PluginsPage.module.css";

/* ── Constants ────────────────────────────────────────────────────── */

const AUTH_TOKEN_KEY = "auth_token";

function getWsUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v1/observability/ws`;
}

/* ── Component ───────────────────────────────────────────────────── */

export function PluginsPage(): JSX.Element {
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toggling, setToggling] = useState<string | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);

  // WebSocket ref
  const wsRef = useRef<ObservabilityWS | null>(null);

  /* ── Data fetching (initial load) ───────────────────────────────── */

  const fetchPlugins = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await pluginsAPI.list();
      setPlugins(data);
    } catch (err) {
      setError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load plugins",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchPlugins();
  }, [fetchPlugins]);

  /* ── WebSocket for live plugin state ────────────────────────────── */

  useEffect(() => {
    const token = localStorage.getItem(AUTH_TOKEN_KEY) || "";
    const ws = new ObservabilityWS(getWsUrl(), token);
    wsRef.current = ws;

    ws.onMessage((msg: Record<string, unknown>) => {
      // Handle per-plugin enable/disable state updates from backend
      if (
        msg.type === "plugin_update" &&
        msg.data &&
        typeof msg.data === "object"
      ) {
        const data = msg.data as Record<string, unknown>;
        if (typeof data.name === "string" && typeof data.enabled === "boolean") {
          setPlugins((prev) =>
            prev.map((p) =>
              p.name === data.name
                ? { ...p, enabled: data.enabled as boolean }
                : p,
            ),
          );
        }
      }
    });

    ws.connect()
      .then(() => {
        ws.subscribe(["plugins"]);
      })
      .catch(() => {
        // WS connection failed — REST fallback works.
      });

    return () => {
      ws.disconnect();
    };
  }, []);

  /* ── Toggle ────────────────────────────────────────────────────── */

  const handleToggle = async (plugin: Plugin): Promise<void> => {
    setToggling(plugin.name);
    setToggleError(null);
    try {
      if (plugin.enabled) {
        await pluginsAPI.disable(plugin.name);
      } else {
        await pluginsAPI.enable(plugin.name);
      }
      await fetchPlugins();
    } catch (err) {
      setToggleError(
        err instanceof Error ? err.message : "Toggle failed",
      );
    } finally {
      setToggling(null);
    }
  };

  /* ── Table columns ─────────────────────────────────────────────── */

  const columns: TableColumn<Plugin>[] = [
    {
      key: "name",
      header: "Plugin",
      sortable: true,
    },
    {
      key: "version",
      header: "Version",
      sortable: true,
    },
    {
      key: "hooks",
      header: "Hooks",
      render: (row: Plugin) => (
        <Badge variant={row.hooks.length > 0 ? "success" : "info"}>
          {row.hooks.length}
        </Badge>
      ),
      sortable: true,
    },
    {
      key: "enabled",
      header: "Enabled",
      render: (row: Plugin) => (
        <Badge variant={row.enabled ? "success" : "info"}>
          {row.enabled ? "Yes" : "No"}
        </Badge>
      ),
      sortable: true,
    },
    {
      key: "actions",
      header: "",
      render: (row: Plugin) => (
        <button
          className={
            row.enabled ? styles.disableBtn : styles.enableBtn
          }
          onClick={() => void handleToggle(row)}
          disabled={toggling === row.name}
          data-testid={`toggle-plugin-${row.name}`}
        >
          {toggling === row.name
            ? "..."
            : row.enabled
              ? "Disable"
              : "Enable"}
        </button>
      ),
      sortable: false,
    },
  ];

  /* ── Render ────────────────────────────────────────────────────── */

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Plugins</h1>
      </div>

      {error && <div className={styles.errorBlock}>{error}</div>}
      {toggleError && <div className={styles.errorBlock}>{toggleError}</div>}

      {loading ? (
        <div className={styles.loading}>Loading plugins...</div>
      ) : (
        <Table
          columns={columns}
          data={plugins}
          defaultSortKey="name"
          defaultSortDirection="asc"
        />
      )}
    </div>
  );
}

export default PluginsPage;
