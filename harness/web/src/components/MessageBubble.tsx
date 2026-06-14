import ReactMarkdown from "react-markdown";
import { ToolCallCard, type ToolCallStatus } from "./ToolCallCard";
import type { MessageRole } from "../api/client";

interface MessageBubbleProps {
  role: MessageRole;
  content: string;
  toolCall?: {
    id: string;
    name: string;
    args: Record<string, unknown>;
    result?: unknown;
    status?: ToolCallStatus;
  };
}

/**
 * One chat message. user bubbles float right, assistant bubbles float left,
 * tool results are full-width and collapsible.
 */
export function MessageBubble({
  role,
  content,
  toolCall,
}: MessageBubbleProps): JSX.Element {
  if (role === "user") {
    return (
      <div className="message message--user">
        <div className="message__bubble message__bubble--user">
          <div className="message__content">{content}</div>
        </div>
      </div>
    );
  }

  if (role === "tool" && toolCall) {
    return (
      <div className="message message--tool">
        <ToolCallCard
          name={toolCall.name}
          args={toolCall.args}
          result={toolCall.result}
          status={toolCall.status ?? "ok"}
        />
      </div>
    );
  }

  // assistant
  return (
    <div className="message message--assistant">
      <div className="message__bubble message__bubble--assistant">
        <div className="message__content">
          <ReactMarkdown>{content}</ReactMarkdown>
        </div>
      </div>
    </div>
  );
}
