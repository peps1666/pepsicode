import type { ChatMessage } from "../stores/session";
import styles from "./ToolActivityItem.module.css";

export default function ToolActivityItem({ message }: { message: ChatMessage }) {
  if (message.role === "progress") {
    return (
      <div className={styles.progress}>
        <span className={styles.progressIcon}>⋯</span>
        <span>{message.content}</span>
      </div>
    );
  }

  const isCall = message.role === "tool_call";
  const details = isCall
    ? message.toolInput ? JSON.stringify(message.toolInput, null, 2) : ""
    : message.content;
  const status = isCall ? "called" : message.isError ? "failed" : "completed";

  return (
    <div className={styles.activity}>
      <div className={styles.activityHeader}>
        <span className={styles.activityIcon}>{isCall ? "↗" : message.isError ? "✕" : "✓"}</span>
        <span className={styles.toolName}>{message.toolName || "tool"}</span>
        <span className={`${styles.status} ${message.isError ? styles.failed : ""}`}>{status}</span>
      </div>
      {details && (
        <div className={styles.detailsBlock}>
          <div className={styles.detailsLabel}>{isCall ? "Arguments" : "Result"}</div>
          <pre className={styles.detailsContent}>{details}</pre>
        </div>
      )}
    </div>
  );
}
