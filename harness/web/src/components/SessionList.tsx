import { useEffect, useState } from "react";
import {
  createSession,
  deleteSession,
  listSessions,
  type Session,
} from "../api/client";

interface SessionListProps {
  currentSessionId: string | null;
  defaultModel: string;
  onSelect: (id: string) => void;
  onChange: () => void; // parent reloads messages / state when sessions list changes
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Left column. Lists sessions, highlights the active one, creates new chats
 * with the default model, and deletes sessions (with a confirm() guard).
 */
export function SessionList({
  currentSessionId,
  defaultModel,
  onSelect,
  onChange,
}: SessionListProps): JSX.Element {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const s = await listSessions();
      setSessions(s);
    } catch (e: unknown) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  const handleNew = async (): Promise<void> => {
    if (!defaultModel) return;
    setCreating(true);
    setError(null);
    try {
      const s = await createSession("New chat", defaultModel);
      await refresh();
      onChange();
      onSelect(s.id);
    } catch (e: unknown) {
      setError(errMsg(e));
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: string): Promise<void> => {
    if (!confirm("Delete this session? This cannot be undone.")) return;
    setError(null);
    try {
      await deleteSession(id);
      const remaining = sessions.filter((x) => x.id !== id);
      setSessions(remaining);
      onChange();
      if (id === currentSessionId) {
        const next = remaining[0]?.id ?? null;
        onSelect(next ?? "");
      }
    } catch (e: unknown) {
      setError(errMsg(e));
    }
  };

  return (
    <aside className="session-list" aria-label="Sessions">
      <div className="session-list__header">
        <span className="session-list__title">Sessions</span>
        <button
          className="session-list__new"
          onClick={handleNew}
          disabled={creating || !defaultModel}
          title={
            defaultModel
              ? "Create a new chat"
              : "Pick a model in the chat first"
          }
        >
          + New chat
        </button>
      </div>

      {error && <div className="session-list__error">{error}</div>}
      {loading && sessions.length === 0 && (
        <div className="session-list__empty">Loading…</div>
      )}
      {!loading && sessions.length === 0 && !error && (
        <div className="session-list__empty">
          No sessions yet. Click "+ New chat" to start.
        </div>
      )}

      <ul className="session-list__items">
        {sessions.map((s) => {
          const active = s.id === currentSessionId;
          return (
            <li
              key={s.id}
              className={
                "session-item" + (active ? " session-item--active" : "")
              }
            >
              <button
                className="session-item__main"
                onClick={() => onSelect(s.id)}
                title={s.id}
              >
                <span className="session-item__title">{s.title}</span>
                <span className="session-item__meta">
                  {s.model} · {s.message_count} msg
                </span>
              </button>
              <button
                className="session-item__delete"
                onClick={() => handleDelete(s.id)}
                aria-label={`Delete ${s.title}`}
                title="Delete session"
              >
                ×
              </button>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
