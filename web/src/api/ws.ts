/**
 * WI-04: Observability WebSocket client.
 *
 * ``ObservabilityWS`` wraps the native ``WebSocket`` API with:
 *
 *   - Auto-reconnect (exponential backoff: 1s, 2s, 4s, 8s, max 30s)
 *   - Heartbeat: ping every 15s, expect pong within 10s
 *   - Topic subscription: ``subscribe(["metrics","health","audit"])``
 *   - Message callback: ``onMessage(callback)``
 *
 * Usage::
 *
 *   const ws = new ObservabilityWS("ws://localhost:8765/api/v1/observability/ws", "mytoken");
 *   ws.onMessage((msg) => console.log(msg));
 *   await ws.connect();
 *   ws.subscribe(["metrics", "health"]);
 *   // ... later
 *   ws.disconnect();
 */

type MessageHandler = (msg: Record<string, unknown>) => void;

const DEFAULT_RECONNECT_INTERVALS_MS = [1000, 2000, 4000, 8000, 30000];
const PING_INTERVAL_MS = 15_000;
const PONG_TIMEOUT_MS = 10_000;

export class ObservabilityWS {
  private readonly _url: string;
  private readonly _token: string;
  private _ws: WebSocket | null = null;
  private _handler: MessageHandler | null = null;
  private _pingTimer: ReturnType<typeof setInterval> | null = null;
  private _pongTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _reconnectAttempt = 0;
  private _intentionalClose = false;
  private _connected = false;

  constructor(url: string, token: string) {
    this._url = url;
    this._token = token;
  }

  /** Open the WebSocket connection. Resolves when connected. */
  connect(): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }

      const fullUrl = `${this._url}?token=${encodeURIComponent(this._token)}`;
      this._intentionalClose = false;

      try {
        this._ws = new WebSocket(fullUrl);
      } catch (err) {
        reject(err);
        return;
      }

      const ws = this._ws;

      const onOpen = () => {
        ws.removeEventListener("open", onOpen);
        ws.removeEventListener("close", onClose);
        ws.removeEventListener("error", onError);

        this._connected = true;
        this._reconnectAttempt = 0;
        this._startHeartbeat();
        resolve();
      };

      const onClose = (event: CloseEvent) => {
        ws.removeEventListener("open", onOpen);
        ws.removeEventListener("close", onClose);
        ws.removeEventListener("error", onError);

        this._connected = false;
        this._stopHeartbeat();

        if (!this._intentionalClose && event.code !== 1000) {
          this._scheduleReconnect();
        }

        if (!this._intentionalClose && this._reconnectAttempt === 0) {
          // First close before connect() resolved — reject.
          reject(new Error(`WebSocket closed: ${event.code} ${event.reason}`));
        }
      };

      const onError = () => {
        ws.removeEventListener("open", onOpen);
        ws.removeEventListener("close", onClose);
        ws.removeEventListener("error", onError);

        if (this._reconnectAttempt === 0) {
          reject(new Error("WebSocket connection error"));
        }
      };

      ws.addEventListener("open", onOpen);
      ws.addEventListener("close", onClose);
      ws.addEventListener("error", onError);

      // Main message handler.
      ws.addEventListener("message", (event: MessageEvent) => {
        try {
          const msg = JSON.parse(event.data as string) as Record<string, unknown>;
          if (msg.type === "pong") {
            this._onPong();
            return;
          }
          this._handler?.(msg);
        } catch {
          // Ignore parse errors.
        }
      });
    });
  }

  /** Gracefully close the connection. No auto-reconnect. */
  disconnect(): void {
    this._intentionalClose = true;
    this._connected = false;
    this._clearReconnect();
    this._stopHeartbeat();
    if (this._ws) {
      this._ws.close(1000, "client disconnect");
      this._ws = null;
    }
  }

  /** Subscribe to a set of topics. Sends a ``subscribe`` message. */
  subscribe(topics: string[]): void {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify({ type: "subscribe", topics }));
    }
  }

  /** Register a message callback. Replaces any previous handler. */
  onMessage(callback: MessageHandler): void {
    this._handler = callback;
  }

  /** True if the WebSocket is currently open. */
  get connected(): boolean {
    return this._ws?.readyState === WebSocket.OPEN && this._connected;
  }

  // ── heartbeat ──────────────────────────────────────────────────────

  private _startHeartbeat(): void {
    this._stopHeartbeat();
    this._pingTimer = setInterval(() => {
      if (this._ws?.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({ type: "ping" }));
        this._pongTimer = setTimeout(() => {
          // Pong not received in time — reconnect.
          if (this._ws?.readyState === WebSocket.OPEN) {
            this._ws.close(4001, "pong timeout");
          }
        }, PONG_TIMEOUT_MS);
      }
    }, PING_INTERVAL_MS);
  }

  private _stopHeartbeat(): void {
    if (this._pingTimer !== null) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
    if (this._pongTimer !== null) {
      clearTimeout(this._pongTimer);
      this._pongTimer = null;
    }
  }

  private _onPong(): void {
    if (this._pongTimer !== null) {
      clearTimeout(this._pongTimer);
      this._pongTimer = null;
    }
  }

  // ── reconnect ──────────────────────────────────────────────────────

  private _scheduleReconnect(): void {
    if (this._intentionalClose || this._reconnectTimer !== null) {
      return;
    }
    const intervals = DEFAULT_RECONNECT_INTERVALS_MS;
    const delay = intervals[Math.min(this._reconnectAttempt, intervals.length - 1)];
    this._reconnectAttempt += 1;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      this.connect().catch(() => {
        // connect() schedules another reconnect via onClose.
      });
    }, delay);
  }

  private _clearReconnect(): void {
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }
}
