// Typed REST client for Solomon Harness backend.
// All requests go through Vite dev-server proxy at /api/* → http://localhost:8765

const API_BASE = "/api";

// === Types — match Python Pydantic schemas in harness/server/ ===

export interface HealthResponse {
  status: string;
  version: string;
  project_root: string;
}

export interface ModelSpec {
  id: string;
  provider: string;
  tier: string;
  ctx: number;
  available: boolean;
  pricing_input: number;
  pricing_output: number;
}

export interface Session {
  id: string;
  title: string;
  model: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  total_tokens: number;
  total_cost: number;
}

export type MessageRole = "user" | "assistant" | "tool";

export interface Message {
  id: string;
  session_id: string;
  role: MessageRole;
  content: string;
  tool_call_id?: string | null;
  tool_calls?: ToolCallRecord[] | null;
  usage?: TokenUsage | null;
  cost?: number | null;
  created_at: string;
}

export interface ToolCallRecord {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: unknown;
  status?: "running" | "ok" | "error";
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

// === Low-level helper ===

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let detail: string;
    try {
      const body = (await res.json()) as { detail?: string };
      detail = body.detail ?? res.statusText;
    } catch {
      detail = res.statusText;
    }
    throw new Error(`HTTP ${res.status} ${res.statusText}: ${detail}`);
  }
  // 204 No Content — caller can ignore the body
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

// === Endpoints ===

export async function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export async function getModels(): Promise<ModelSpec[]> {
  return request<ModelSpec[]>("/models");
}

export async function listSessions(): Promise<Session[]> {
  return request<Session[]>("/sessions");
}

export async function createSession(
  title: string,
  model: string,
): Promise<Session> {
  return request<Session>("/sessions", {
    method: "POST",
    body: JSON.stringify({ title, model }),
  });
}

export async function deleteSession(id: string): Promise<void> {
  await request<void>(`/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function getMessages(sessionId: string): Promise<Message[]> {
  return request<Message[]>(
    `/sessions/${encodeURIComponent(sessionId)}/messages`,
  );
}
