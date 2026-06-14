// WebSocket chat client — Phase 0 Step 10.
// Real streaming client over the Vite-dev-server proxy.
//
// Endpoint: /api/chat/ws (proxied by Vite → ws://localhost:8765/api/chat/ws)
// Wire format: client sends { "type": "user_message", "content": "..." } as
// the first frame after `onopen`; server pushes a sequence of ChatEvent
// JSON objects until it emits a terminal `session_done` (or `error`).

export interface ChatEventAssistantMessage {
  type: "assistant_message";
  content: string;
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  cost?: number;
}

export interface ChatEventToolResult {
  type: "tool_result";
  tool_call: { id: string; name: string; args: Record<string, unknown> };
  result?: unknown;
  status: "running" | "ok" | "error";
}

export interface ChatEventDone {
  type: "done";
}

export interface ChatEventError {
  type: "error";
  content?: string;
}

export interface ChatEventSessionDone {
  type: "session_done";
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  cost?: number;
}

export type ChatEvent =
  | ChatEventAssistantMessage
  | ChatEventToolResult
  | ChatEventDone
  | ChatEventError
  | ChatEventSessionDone;

export interface ChatRequest {
  session_id: string;
  model: string;
  content: string;
}

/**
 * Returns the absolute WebSocket URL the browser should connect to.
 *
 * We always go through the Vite dev server (or the production origin) so the
 * `ws: true` proxy rule in vite.config.ts forwards the upgrade to
 * ws://localhost:8765/api/chat/ws.
 */
export function wsEndpoint(sessionId: string, model: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams({
    session_id: sessionId,
    model,
  });
  return `${proto}//${window.location.host}/api/chat/ws?${params.toString()}`;
}

/**
 * Open a WebSocket, send the user message, and yield ChatEvents until the
 * server terminates the stream (session_done / error / socket close).
 *
 * Backed by a real WebSocket — no polling. Callers can `break` out of the
 * `for await (...)` loop to abort consumption; pass an `AbortSignal` to also
 * close the underlying socket.
 */
export async function* chatStream(
  sessionId: string,
  model: string,
  content: string,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const ws = new WebSocket(wsEndpoint(sessionId, model));

  // Pull model: the server streams ChatEvents into queue, donePromise resolves
  // when the terminal frame arrives or the socket closes/errors.
  const queue: ChatEvent[] = [];
  let resolveNext: (() => void) | null = null;
  let doneResolve: (() => void) | null = null;

  const wakeup = () => {
    const r = resolveNext;
    resolveNext = null;
    if (r) r();
  };

  const donePromise = new Promise<void>((resolve) => {
    doneResolve = resolve;
  });

  const fail = (e: Event) => {
    void e;
    doneResolve?.();
  };

  ws.onopen = () => {
    const payload: ChatRequest & { type: "user_message" } = {
      type: "user_message",
      session_id: sessionId,
      model,
      content,
    };
    try {
      ws.send(JSON.stringify(payload));
    } catch {
      // socket already in CLOSING/CLOSED — let onclose resolve donePromise
    }
  };

  ws.onmessage = (e: MessageEvent<string>) => {
    let evt: ChatEvent;
    try {
      evt = JSON.parse(e.data) as ChatEvent;
    } catch {
      return;
    }
    queue.push(evt);
    if (evt.type === "session_done" || evt.type === "error") {
      doneResolve?.();
    }
    // Always wake the consumer — both for incremental frames and for the
    // terminal frame, since the consumer will drain the queue and then exit.
    wakeup();
  };

  ws.onerror = fail;
  ws.onclose = () => {
    doneResolve?.();
  };

  if (signal) {
    const onAbort = () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
      doneResolve?.();
    };
    if (signal.aborted) {
      onAbort();
    } else {
      signal.addEventListener("abort", onAbort, { once: true });
    }
  }

  try {
    while (true) {
      if (queue.length > 0) {
        const evt = queue.shift();
        if (evt) yield evt;
        continue;
      }
      // Queue empty — wait for either a wakeup or a terminal event.
      if (doneResolve === null) {
        // already finished
        break;
      }
      await Promise.race<void>([
        new Promise<void>((r) => {
          resolveNext = r;
        }),
        donePromise,
      ]);
      if (queue.length === 0) {
        // donePromise won the race; no more frames coming
        break;
      }
    }
  } finally {
    try {
      ws.close();
    } catch {
      // ignore
    }
  }
}
