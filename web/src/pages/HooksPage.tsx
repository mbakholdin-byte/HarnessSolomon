/**
 * WI-05: Event Hooks page — admin interface for the hook subsystem.
 *
 * Lists all registered hook events, shows configuration details in a
 * modal, and provides enable/disable toggles.
 *
 * WI-05: WebSocket integration — replaces polling for live hook on/off state.
 * Initial data loaded via REST; subsequent state changes arrive via WS
 * (topic: "hooks").
 */

import { useState, useEffect, useCallback, useRef } from "react";
import {
  hooksAPI,
  type HookEvent,
  type HookConfig,
} from "../api/hooks";
import { APIError } from "../api/types";
import { ObservabilityWS } from "../api/ws";
import { Table, Modal, Badge } from "../components";
import type { TableColumn } from "../components";
import styles from "./HooksPage.module.css";

/* ── Constants ────────────────────────────────────────────────────── */

const AUTH_TOKEN_KEY = "auth_token";

function getWsUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v1/observability/ws`;
}

/* ── Helpers ─────────────────────────────────────────────────────── */

function formatTimeout(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

/* ── Component ───────────────────────────────────────────────────── */

export function HooksPage(): JSX.Element {
  const [events, setEvents] = useState<HookEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Config modal
  const [selectedEvent, setSelectedEvent] = useState<HookEvent | null>(null);
  const [config, setConfig] = useState<HookConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);

  // WebSocket ref
  const wsRef = useRef<ObservabilityWS | null>(null);

  /* ── Data fetching (initial load) ───────────────────────────────── */

  const fetchEvents = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await hooksAPI.listEvents();
      setEvents(data);
    } catch (err) {
      setError(
        err instanceof APIError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to load hook events",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchEvents();
  }, [fetchEvents]);

  /* ── WebSocket for live hook state ──────────────────────────────── */

  useEffect(() => {
    const token = localStorage.getItem(AUTH_TOKEN_KEY) || "";
    const ws = new ObservabilityWS(getWsUrl(), token);
    wsRef.current = ws;

    ws.onMessage((msg: Record<string, unknown>) => {
      // Handle hook config state updates from backend
      if (
        msg.type === "hooks_config" &&
        msg.data &&
        typeof msg.data === "object"
      ) {
        const data = msg.data as Record<string, unknown>;
        if (typeof data.enabled === "boolean") {
          setConfig((prev) =>
            prev ? { ...prev, enabled: data.enabled as boolean } : prev,
          );
        }
      }
    });

    ws.connect()
      .then(() => {
        ws.subscribe(["hooks"]);
      })
      .catch(() => {
        // WS connection failed — no UI feedback needed, REST fallback works.
      });

    return () => {
      ws.disconnect();
    };
  }, []);

  /* ── Config modal ──────────────────────────────────────────────── */

  const openConfig = async (event: HookEvent): Promise<void> => {
    setSelectedEvent(event);
    setConfigLoading(true);
    setConfigError(null);
    try {
      const cfg = await hooksAPI.getConfig();
      setConfig(cfg);
    } catch (err) {
      setConfigError(
        err instanceof Error ? err.message : "Failed to load config",
      );
    } finally {
      setConfigLoading(false);
    }
  };

  const closeConfig = (): void => {
    setSelectedEvent(null);
    setConfig(null);
    setConfigError(null);
  };

  const toggleHooks = async (enable: boolean): Promise<void> => {
    setToggling(true);
    setConfigError(null);
    try {
      if (enable) {
        await hooksAPI.enable();
      } else {
        await hooksAPI.disable();
      }
      // Refresh config after toggle (WS will also push update)
      const cfg = await hooksAPI.getConfig();
      setConfig(cfg);
    } catch (err) {
      setConfigError(
        err instanceof Error ? err.message : "Toggle failed",
      );
    } finally {
      setToggling(false);
    }
  };

  /* ── Table columns ─────────────────────────────────────────────── */

  const columns: TableColumn<HookEvent>[] = [
    {
      key: "name",
      header: "Event",
      sortable: true,
    },
    {
      key: "description",
      header: "Description",
      sortable: false,
    },
    {
      key: "callback_count",
      header: "Callbacks",
      render: (row: HookEvent) => (
        <Badge variant={row.callback_count > 0 ? "success" : "info"}>
          {row.callback_count}
        </Badge>
      ),
      sortable: true,
    },
  ];

  /* ── Render ────────────────────────────────────────────────────── */

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Event Hooks</h1>
      </div>

      {error && <div className={styles.errorBlock}>{error}</div>}

      {loading ? (
        <div className={styles.loading}>Loading hook events...</div>
      ) : (
        <Table
          columns={columns}
          data={events}
          defaultSortKey="name"
          defaultSortDirection="asc"
          onSort={() => {
            // Sort handled internally by Table component
          }}
        />
      )}

      {/* ── Config Modal ──────────────────────────────────────────── */}
      <Modal
        open={selectedEvent !== null}
        onClose={closeConfig}
        title={
          selectedEvent
            ? `Hook Config — ${selectedEvent.name}`
            : "Hook Config"
        }
      >
        {configLoading ? (
          <div className={styles.loading}>Loading config...</div>
        ) : configError ? (
          <div className={styles.errorBlock}>{configError}</div>
        ) : config ? (
          <>
            <div className={styles.configSection}>
              <h4 className={styles.configTitle}>Event Info</h4>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Event</span>
                <span className={styles.configValue}>
                  {selectedEvent?.name}
                </span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Description</span>
                <span className={styles.configValue}>
                  {selectedEvent?.description}
                </span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Callbacks</span>
                <span className={styles.configValue}>
                  {selectedEvent?.callback_count}
                </span>
              </div>
            </div>

            <div className={styles.configSection}>
              <h4 className={styles.configTitle}>System Config</h4>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Enabled</span>
                <span className={styles.configValue}>
                  {config.enabled ? "Yes" : "No"}
                </span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Audit Log</span>
                <span className={styles.configValue}>
                  {config.audit_log ? "Yes" : "No"}
                </span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Elicitation</span>
                <span className={styles.configValue}>
                  {config.elicitation_enabled ? "Yes" : "No"}
                </span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>SSE</span>
                <span className={styles.configValue}>
                  {config.elicitation_sse_enabled ? "Yes" : "No"}
                </span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Longpoll</span>
                <span className={styles.configValue}>
                  {config.elicitation_longpoll_enabled ? "Yes" : "No"}
                </span>
              </div>
              <div className={styles.configRow}>
                <span className={styles.configLabel}>Timeout</span>
                <span className={styles.configValue}>
                  {formatTimeout(config.default_timeout_ms)}
                </span>
              </div>
            </div>

            <div className={styles.toggleGroup}>
              <button
                className={styles.enableBtn}
                onClick={() => void toggleHooks(true)}
                disabled={toggling || config.enabled}
                data-testid="hooks-enable-btn"
              >
                Enable All
              </button>
              <button
                className={styles.disableBtn}
                onClick={() => void toggleHooks(false)}
                disabled={toggling || !config.enabled}
                data-testid="hooks-disable-btn"
              >
                Disable All
              </button>
            </div>
          </>
        ) : null}
      </Modal>
    </div>
  );
}

export default HooksPage;
