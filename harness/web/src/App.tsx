import { useEffect, useState } from "react";
import {
  createSession,
  deleteSession,
  getHealth,
  getModels,
  listSessions,
  type HealthResponse,
  type ModelSpec,
  type Session,
} from "./api/client";

type Status =
  | { kind: "idle" }
  | { kind: "loading"; label: string }
  | { kind: "ok"; label: string; detail?: string }
  | { kind: "err"; label: string; detail?: string };

const initialStatus: Status = { kind: "idle" };

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [models, setModels] = useState<ModelSpec[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [status, setStatus] = useState<Status>(initialStatus);
  const [newTitle, setNewTitle] = useState("New chat");
  const [newModel, setNewModel] = useState<string>("");

  const pingHealth = async () => {
    setStatus({ kind: "loading", label: "Pinging /api/health…" });
    try {
      const h = await getHealth();
      setHealth(h);
      setStatus({
        kind: "ok",
        label: "Health OK",
        detail: `${h.status} · v${h.version}`,
      });
    } catch (e) {
      setStatus({ kind: "err", label: "Health failed", detail: errMsg(e) });
    }
  };

  const loadModels = async () => {
    setStatus({ kind: "loading", label: "Loading /api/models…" });
    try {
      const m = await getModels();
      setModels(m);
      if (!newModel && m.length > 0) {
        const firstAvail = m.find((x) => x.available) ?? m[0];
        if (firstAvail) setNewModel(firstAvail.id);
      }
      setStatus({
        kind: "ok",
        label: `Loaded ${m.length} model(s)`,
        detail: m.filter((x) => x.available).length + " available",
      });
    } catch (e) {
      setStatus({ kind: "err", label: "Models failed", detail: errMsg(e) });
    }
  };

  const refreshSessions = async () => {
    try {
      const s = await listSessions();
      setSessions(s);
    } catch (e) {
      setStatus({ kind: "err", label: "Sessions failed", detail: errMsg(e) });
    }
  };

  const handleCreate = async () => {
    if (!newModel) return;
    setStatus({ kind: "loading", label: "Creating session…" });
    try {
      await createSession(newTitle, newModel);
      setStatus({ kind: "ok", label: "Session created" });
      await refreshSessions();
    } catch (e) {
      setStatus({ kind: "err", label: "Create failed", detail: errMsg(e) });
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteSession(id);
      await refreshSessions();
    } catch (e) {
      setStatus({ kind: "err", label: "Delete failed", detail: errMsg(e) });
    }
  };

  // Auto-ping health on first mount so the UI never sits empty.
  useEffect(() => {
    void pingHealth();
    void refreshSessions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1 className="app__title">Solomon Harness — Phase 0</h1>
          <p className="app__subtitle">
            Vite + React scaffold · /api proxy → :8765 · Step 9
          </p>
        </div>
        <div className="toolbar">
          <button onClick={pingHealth}>Ping Backend</button>
          <button onClick={loadModels}>List Models</button>
          <button onClick={refreshSessions}>Refresh Sessions</button>
        </div>
      </header>

      <main className="app__body">
        <section className="panel" aria-label="Sidebar">
          <h2 className="panel__title">Status</h2>
          <StatusBlock status={status} />

          <h2 className="panel__title">Health</h2>
          {health ? (
            <pre>{JSON.stringify(health, null, 2)}</pre>
          ) : (
            <div className="status">No data yet — click Ping Backend.</div>
          )}

          <h2 className="panel__title">New session</h2>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <input
              type="text"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="Session title"
              style={inputStyle}
            />
            <select
              value={newModel}
              onChange={(e) => setNewModel(e.target.value)}
              style={inputStyle}
            >
              {models.length === 0 && <option value="">(load models first)</option>}
              {models.map((m) => (
                <option key={m.id} value={m.id} disabled={!m.available}>
                  {m.id} · {m.tier} {m.available ? "" : "(no key)"}
                </option>
              ))}
            </select>
            <button
              onClick={handleCreate}
              disabled={!newModel || !newTitle.trim()}
            >
              Create session
            </button>
          </div>
        </section>

        <section className="panel" aria-label="Main">
          <h2 className="panel__title">
            Models ({models.length})
          </h2>
          {models.length === 0 ? (
            <div className="status">
              No models loaded yet. Click "List Models".
            </div>
          ) : (
            <ul className="model-list">
              {models.map((m) => (
                <li key={m.id} className="model-item">
                  <span>
                    <span className="model-item__id">{m.id}</span>
                    <span className="model-item__tier">{m.tier}</span>
                  </span>
                  <span
                    className={
                      "model-item__avail " +
                      (m.available
                        ? "model-item__avail--yes"
                        : "model-item__avail--no")
                    }
                  >
                    {m.available ? "available" : "no API key"} · ctx{" "}
                    {m.ctx.toLocaleString()}
                  </span>
                </li>
              ))}
            </ul>
          )}

          <h2 className="panel__title">
            Sessions ({sessions.length})
          </h2>
          {sessions.length === 0 ? (
            <div className="status">
              No sessions yet. Use the form on the left to create one.
            </div>
          ) : (
            <ul className="model-list">
              {sessions.map((s) => (
                <li key={s.id} className="model-item">
                  <span style={{ display: "flex", flexDirection: "column" }}>
                    <span className="model-item__id">{s.title}</span>
                    <span style={{ fontSize: 11, color: "var(--fg-dim)" }}>
                      {s.model} · {s.message_count} msg · {s.id.slice(0, 8)}…
                    </span>
                  </span>
                  <button onClick={() => handleDelete(s.id)}>Delete</button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  );
}

function StatusBlock({ status }: { status: Status }) {
  if (status.kind === "idle") return <div className="status">Idle</div>;
  if (status.kind === "loading")
    return <div className="status">⏳ {status.label}</div>;
  if (status.kind === "ok")
    return (
      <div className="status status--ok">
        ✓ {status.label}
        {status.detail ? ` — ${status.detail}` : ""}
      </div>
    );
  return (
    <div className="status status--err">
      ✗ {status.label}
      {status.detail ? ` — ${status.detail}` : ""}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  background: "var(--bg)",
  color: "var(--fg)",
  border: "1px solid var(--border)",
  borderRadius: "var(--radius)",
  padding: "6px 8px",
  font: "inherit",
};

function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}
