import { useEffect, useRef, useState } from "react";
import { getMessages, type Message, type TokenUsage } from "../api/client";
import { chatStream, type ChatEvent } from "../api/ws";
import { MessageBubble } from "./MessageBubble";
import { ModelSelector } from "./ModelSelector";
import { InputBar } from "./InputBar";

interface ChatViewProps {
  sessionId: string | null;
  defaultModel: string;
  onModelChange: (m: string) => void;
}

// Mutable view-model we keep in state. The "id" for an in-flight assistant
// message is a client-generated UUID — the server's `session_done` may
// carry the canonical id later, but for streaming we just need a stable key.
type DisplayMessage = Message & {
  // Local-only fields:
  pending?: boolean;
  streaming?: boolean;
};

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `local-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function usageFromEvent(
  e: ChatEvent,
): TokenUsage | undefined {
  if (e.type === "assistant_message" && e.usage) return e.usage;
  if (e.type === "session_done" && e.usage) return e.usage;
  return undefined;
}

function costFromEvent(e: ChatEvent): number | undefined {
  if (e.type === "assistant_message" && typeof e.cost === "number")
    return e.cost;
  if (e.type === "session_done" && typeof e.cost === "number") return e.cost;
  return undefined;
}

/**
 * Right column. Owns the message list for the active session, loads
 * historical messages on session switch, and runs a real WebSocket stream
 * when the user sends a new message.
 */
export function ChatView({
  sessionId,
  defaultModel,
  onModelChange,
}: ChatViewProps): JSX.Element {
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Load history when session changes.
  useEffect(() => {
    if (!sessionId) {
      setMessages([]);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const m = await getMessages(sessionId);
        if (cancelled) return;
        setMessages(
          m.map((x) => ({
            ...x,
            tool_calls: x.tool_calls ?? null,
            tool_call_id: x.tool_call_id ?? null,
          })),
        );
      } catch (e: unknown) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Auto-scroll on new messages / streaming deltas.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  const handleSend = async (content: string): Promise<void> => {
    if (!sessionId || !defaultModel) return;
    if (streaming) return;

    // Optimistically append the user message.
    const userMsg: DisplayMessage = {
      id: uuid(),
      session_id: sessionId,
      role: "user",
      content,
      created_at: new Date().toISOString(),
    };
    // Placeholder assistant message we mutate as deltas arrive.
    const assistantId = uuid();
    const assistantMsg: DisplayMessage = {
      id: assistantId,
      session_id: sessionId,
      role: "assistant",
      content: "",
      created_at: new Date().toISOString(),
      pending: true,
      streaming: true,
    };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);

    const ac = new AbortController();
    abortRef.current = ac;
    setStreaming(true);
    setError(null);

    try {
      for await (const evt of chatStream(
        sessionId,
        defaultModel,
        content,
        ac.signal,
      )) {
        applyEvent(assistantId, evt, setMessages);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                content:
                  (m.content ? m.content + "\n\n" : "") +
                  `[stream error] ${
                    e instanceof Error ? e.message : String(e)
                  }`,
                streaming: false,
                pending: false,
              }
            : m,
        ),
      );
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const handleAbort = (): void => {
    abortRef.current?.abort();
  };

  if (!sessionId) {
    return (
      <section className="chat-view chat-view--empty" aria-label="Chat">
        <div className="chat-view__placeholder">
          Select a session on the left, or click <b>+ New chat</b> to start.
        </div>
      </section>
    );
  }

  return (
    <section className="chat-view" aria-label="Chat">
      <header className="chat-view__header">
        <div className="chat-view__model">
          <span className="chat-view__label">Model:</span>
          <ModelSelector
            value={defaultModel}
            onChange={onModelChange}
            disabled={streaming}
          />
        </div>
        {streaming && (
          <button
            className="chat-view__abort"
            onClick={handleAbort}
            title="Stop streaming"
          >
            Stop
          </button>
        )}
      </header>

      <div className="chat-view__messages" ref={scrollRef}>
        {loading && messages.length === 0 && (
          <div className="chat-view__placeholder">Loading history…</div>
        )}
        {error && messages.length === 0 && (
          <div className="chat-view__error">Failed to load: {error}</div>
        )}
        {!loading && !error && messages.length === 0 && (
          <div className="chat-view__placeholder">
            No messages yet. Send the first one below.
          </div>
        )}
        {messages.map((m) => {
          // Render tool calls as separate tool bubbles.
          if (m.role === "assistant" && m.tool_calls && m.tool_calls.length > 0) {
            return (
              <div key={m.id} className="message-group">
                {m.tool_calls.map((tc) => (
                  <MessageBubble
                    key={`${m.id}-tc-${tc.id}`}
                    role="tool"
                    content=""
                    toolCall={{
                      id: tc.id,
                      name: tc.name,
                      args: tc.args,
                      result: tc.result,
                      status: tc.status ?? "ok",
                    }}
                  />
                ))}
                {m.content && (
                  <MessageBubble role="assistant" content={m.content} />
                )}
              </div>
            );
          }
          return (
            <MessageBubble
              key={m.id}
              role={m.role}
              content={
                m.streaming
                  ? m.content + (m.pending ? " ▍" : "")
                  : m.content
              }
              toolCall={
                m.tool_calls && m.tool_calls.length > 0
                  ? {
                      id: m.tool_calls[0]!.id,
                      name: m.tool_calls[0]!.name,
                      args: m.tool_calls[0]!.args,
                      result: m.tool_calls[0]!.result,
                      status: m.tool_calls[0]!.status ?? "ok",
                    }
                  : undefined
              }
            />
          );
        })}
      </div>

      <footer className="chat-view__footer">
        <InputBar
          onSend={handleSend}
          disabled={streaming}
          placeholder={
            streaming ? "Waiting for the model…" : "Type a message, Enter to send"
          }
        />
      </footer>
    </section>
  );
}

function applyEvent(
  assistantId: string,
  evt: ChatEvent,
  setMessages: React.Dispatch<React.SetStateAction<DisplayMessage[]>>,
): void {
  setMessages((prev) => {
    const idx = prev.findIndex((m) => m.id === assistantId);
    if (idx === -1) return prev;
    const cur = prev[idx]!;
    let next: DisplayMessage = cur;

    if (evt.type === "assistant_message") {
      const u = usageFromEvent(evt);
      const c = costFromEvent(evt);
      next = {
        ...cur,
        content: cur.content + (evt.content ?? ""),
        usage: u ?? cur.usage,
        cost: c ?? cur.cost,
      };
    } else if (evt.type === "tool_result") {
      const tc = {
        id: evt.tool_call.id,
        name: evt.tool_call.name,
        args: evt.tool_call.args,
        result: evt.result,
        status: evt.status,
      };
      const tcs = [...(cur.tool_calls ?? []), tc];
      next = { ...cur, tool_calls: tcs };
    } else if (evt.type === "done") {
      next = { ...cur, pending: false, streaming: false };
    } else if (evt.type === "session_done") {
      const u = usageFromEvent(evt);
      const c = costFromEvent(evt);
      next = {
        ...cur,
        streaming: false,
        pending: false,
        usage: u ?? cur.usage,
        cost: c ?? cur.cost,
      };
    } else if (evt.type === "error") {
      const errText = evt.content ? `\n[error] ${evt.content}` : "\n[error]";
      next = {
        ...cur,
        content: cur.content + errText,
        streaming: false,
        pending: false,
      };
    }

    const out = prev.slice();
    out[idx] = next;
    return out;
  });
}
