/**
 * WI-04: Plugins page — manage loaded plugins.
 *
 * Lists all plugins with enable/disable toggles. Uses the Table and
 * Badge components from the shared library.
 */

import { useState, useEffect, useCallback } from "react";
import { pluginsAPI, type Plugin } from "../api/plugins";
import { APIError } from "../api/types";
import { Table, Badge } from "../components";
import type { TableColumn } from "../components";
import styles from "./PluginsPage.module.css";

/* ── Component ───────────────────────────────────────────────────── */

export function PluginsPage(): JSX.Element {
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toggling, setToggling] = useState<string | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);

  /* ── Data fetching ─────────────────────────────────────────────── */

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
