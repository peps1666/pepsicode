import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSessionStore, type ChatMessage } from "../stores/session";
import styles from "./MessageItem.module.css";

export default function MessageItem({ message }: { message: ChatMessage }) {
  const { isRunning, branchUpdating, forkSession, regenerate } = useSessionStore();
  if (message.isError) {
    return (
      <div className={styles.errorMessage} role="alert">
        <div className={styles.errorHeader}>请求失败</div>
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
        {message.entryId && <div className={styles.branchActions}>
          <button disabled={isRunning || branchUpdating} onClick={() => { void forkSession(message.entryId!).catch(() => {}); }}>从此分叉</button>
          <button disabled={isRunning || branchUpdating} title="再次调用模型，可能重新执行工具；文件不会回滚"
            onClick={() => { void regenerate(message.entryId!).catch(() => {}); }}>重新生成</button>
        </div>}
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
