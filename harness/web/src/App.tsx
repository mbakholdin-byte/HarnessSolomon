import { useEffect, useState } from "react";
import { getHealth, type HealthResponse } from "./api/client";
import { SessionList } from "./components/SessionList";
import { ChatView } from "./components/ChatView";

/**
 * Root layout: 2-column shell.
 *   - left:   SessionList (300px)
 *   - right:  ChatView (flex: 1) — owns messages, model, streaming
 *
 * `currentSessionId` and `currentModel` live here so the sidebar can both
 * highlight the active row and seed "New chat" with the same model the
 * chat view will use.
 */
export function App(): JSX.Element {
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [currentModel, setCurrentModel] = useState<string>("");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthErr, setHealthErr] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const h = await getHealth();
        setHealth(h);
      } catch (e: unknown) {
        setHealthErr(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  return (
    <div className="app app--chat">
      <header className="app__header">
        <div>
          <h1 className="app__title">Solomon Harness</h1>
          <p className="app__subtitle">
            Phase 0 · chat · {health ? `v${health.version}` : "…"}
            {healthErr && <span className="app__health-err"> · backend offline</span>}
          </p>
        </div>
        <div className="toolbar">
          {health ? (
            <span className="app__health" title={JSON.stringify(health)}>
              ● {health.status}
            </span>
          ) : (
            <span className="app__health app__health--err">○ offline</span>
          )}
        </div>
      </header>

      <main className="app__body app__body--chat">
        <SessionList
          currentSessionId={currentSessionId}
          defaultModel={currentModel}
          onSelect={(id) => setCurrentSessionId(id || null)}
          onChange={() => {
            /* sessions list is its own source of truth; nothing else to do */
          }}
        />
        <ChatView
          sessionId={currentSessionId}
          defaultModel={currentModel}
          onModelChange={setCurrentModel}
        />
      </main>
    </div>
  );
}
