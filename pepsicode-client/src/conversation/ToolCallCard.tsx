import { useState } from "react";
import type { ChatMessage } from "../stores/session";
import styles from "./ToolCallCard.module.css";

export default function ToolCallCard({ message }: { message: ChatMessage }) {
  const [expanded, setExpanded] = useState(true);

  const inputStr = message.toolInput
    ? JSON.stringify(message.toolInput, null, 2)
    : "";

  return (
    <div className={styles.card}>
      <div className={styles.header} onClick={() => setExpanded(!expanded)}>
        <span className={styles.chevron}>{expanded ? "▾" : "▸"}</span>
        <span className={styles.toolIcon}>🔧</span>
        <span className={styles.toolName}>{message.toolName}</span>
        <span className={styles.status}>running</span>
      </div>
      {expanded && inputStr && (
        <div className={styles.inputBlock}>
          <div className={styles.inputLabel}>Arguments</div>
          <pre className={styles.inputContent}>{inputStr}</pre>
        </div>
      )}
    </div>
  );
}
