import { useState, useRef, useCallback, type KeyboardEvent } from "react";
import styles from "./Composer.module.css";

interface ComposerProps {
  onSend: (input: string) => void;
  disabled: boolean;
}

export default function Composer({ onSend, disabled }: ComposerProps) {
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = useCallback(() => {
    const trimmed = input.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setInput("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [input, disabled, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  const handleInput = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, []);

  return (
    <div className={styles.container}>
      <div className={styles.inputWrapper}>
        <textarea
          ref={textareaRef}
          className={styles.textarea}
          value={input}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "Running..." : "Ask anything, or type / for commands"}
          disabled={disabled}
          rows={1}
          autoFocus
        />
        <button className={styles.sendButton} onClick={handleSend} disabled={disabled || !input.trim()}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
          </svg>
        </button>
      </div>
      <div className={styles.hint}>Enter to send • Shift+Enter for new line</div>
    </div>
  );
}
