import { useState, type KeyboardEvent } from "react";

interface InputBarProps {
  onSend: (content: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

/**
 * Plain <textarea> + Send button. Enter sends, Shift+Enter inserts a newline.
 * Auto-resizes vertically up to ~6 lines.
 */
export function InputBar({
  onSend,
  disabled = false,
  placeholder = "Message…",
}: InputBarProps): JSX.Element {
  const [value, setValue] = useState("");

  const submit = (): void => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="input-bar">
      <textarea
        className="input-bar__field"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKey}
        placeholder={placeholder}
        rows={1}
        disabled={disabled}
      />
      <button
        className="input-bar__send"
        onClick={submit}
        disabled={disabled || value.trim().length === 0}
        aria-label="Send"
      >
        Send
      </button>
    </div>
  );
}
