import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../stores/session";
import ToolCallCard from "./ToolCallCard";
import styles from "./MessageItem.module.css";

export default function MessageItem({ message }: { message: ChatMessage }) {
  if (message.role === "tool_call") {
    return <ToolCallCard message={message} />;
  }

  if (message.role === "tool_result") {
    return (
      <div className={styles.toolResult}>
        <div className={styles.toolResultHeader}>
          <span className={styles.toolResultIcon}>{message.isError ? "✕" : "✓"}</span>
          <span className={styles.toolResultName}>{message.toolName}</span>
        </div>
        <pre className={styles.toolResultContent}>{message.content}</pre>
      </div>
    );
  }

  if (message.role === "progress") {
    return (
      <div className={styles.progress}>
        <span className={styles.progressIcon}>⋯</span>
        <span className={styles.progressContent}>{message.content}</span>
      </div>
    );
  }

  const isUser = message.role === "user";
  const className = isUser ? styles.userMessage : styles.assistantMessage;

  return (
    <div className={className}>
      <div className={styles.roleLabel}>{isUser ? "You" : "Assistant"}</div>
      <div className={styles.content}>
        {isUser ? (
          <p className={styles.userText}>{message.content}</p>
        ) : (
          <div className={styles.markdown}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content || (message.isStreaming ? "▋" : "")}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}
