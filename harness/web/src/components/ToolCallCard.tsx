import { useState } from "react";

export type ToolCallStatus = "running" | "ok" | "error";

interface ToolCallCardProps {
  name: string;
  args: Record<string, unknown>;
  result?: unknown;
  status: ToolCallStatus;
}

const RESULT_MAX = 500;

function safeStringify(v: unknown, max: number): string {
  try {
    const s = JSON.stringify(v, null, 2);
    if (s.length <= max) return s;
    return s.slice(0, max) + `\n… (truncated, ${s.length - max} chars more)`;
  } catch {
    return String(v);
  }
}

function statusBadge(s: ToolCallStatus): string {
  if (s === "running") return "⏳";
  if (s === "ok") return "✓";
  return "✗";
}

/**
 * Collapsible card showing a tool invocation: name + status badge in the
 * header, expandable body with pretty-printed args and (truncated) result.
 */
export function ToolCallCard({
  name,
  args,
  result,
  status,
}: ToolCallCardProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const argsStr = safeStringify(args, 1000);
  const resultStr =
    result === undefined ? "(no result yet)" : safeStringify(result, RESULT_MAX);

  return (
    <details
      className={`tool-call tool-call--${status}`}
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="tool-call__summary">
        <span className="tool-call__icon">{statusBadge(status)}</span>
        <span className="tool-call__name">🔧 {name}</span>
        <span className="tool-call__status">{status}</span>
      </summary>
      <div className="tool-call__body">
        <div className="tool-call__section">
          <div className="tool-call__label">args</div>
          <pre className="tool-call__pre">{argsStr}</pre>
        </div>
        <div className="tool-call__section">
          <div className="tool-call__label">result</div>
          <pre className="tool-call__pre">{resultStr}</pre>
        </div>
      </div>
    </details>
  );
}
