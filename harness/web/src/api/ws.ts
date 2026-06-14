// WebSocket chat client — STUB for Phase 0 Step 9.
// Real streaming implementation lands in Step 7 (Frontend chat UI).
//
// Endpoint: /ws/chat (proxied by Vite → ws://localhost:8765/ws/chat)
// Wire format: incoming JSON ChatEvent objects, sent as { session_id, model, content }.
//
// In Step 9 we only expose the signature + a helper that returns a labeled
// async generator so the UI can verify the proxy/ws plumbing is wired correctly
// without committing to a final message shape.

export interface ChatEvent {
  type: "delta" | "tool_call" | "tool_result" | "done" | "error";
  data: unknown;
}

export interface ChatRequest {
  session_id: string;
  model: string;
  content: string;
}

/**
 * Open a WebSocket and yield ChatEvents from the server.
 *
 * STUB: in Step 9 this throws "not implemented" to keep the surface honest
 * without dragging the full agent-loop contract from Python before Step 7.
 */
export async function* chatStream(
  _sessionId: string,
  _model: string,
  _content: string,
): AsyncIterator<ChatEvent> {
  void _sessionId;
  void _model;
  void _content;
  throw new Error(
    "chatStream() is a Step-9 stub. Full implementation arrives in Step 7.",
  );
}

/**
 * Helper: returns true if the WebSocket endpoint is reachable through the
 * Vite proxy. Step 7 will replace this with a real ping frame.
 */
export function wsEndpoint(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/chat`;
}
