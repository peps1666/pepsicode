import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../stores/session";
import styles from "./MessageItem.module.css";

export default function MessageItem({ message }: { message: ChatMessage }) {
  if (message.isError) {
    return (
      <div className={styles.errorMessage} role="alert">
        <div className={styles.errorHeader}>Request failed</div>
        <div className={styles.errorContent}>{message.content}</div>
      </div>
    );
  }

  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div className={styles.userRow}>
        <div className={styles.bubble}>
          <p className={styles.userText}>{message.content}</p>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.assistantMessage}>
      <div className={styles.markdown}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content || (message.isStreaming ? "▋" : "")}</ReactMarkdown>
      </div>
    </div>
  );
}
