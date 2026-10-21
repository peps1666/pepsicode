import { useEffect, useState } from "react";
import type { ChatMessage } from "../stores/session";
import styles from "./ModelThinking.module.css";

export default function ModelThinking({ message }: { message: ChatMessage }) {
  const [expanded, setExpanded] = useState(Boolean(message.isStreaming));
  useEffect(() => { if (message.isStreaming) setExpanded(true); }, [message.isStreaming]);
  const status = message.isStreaming ? "思考中…" : message.thinkingStatus === "cancelled" ? "已停止" : message.thinkingStatus === "interrupted" ? "已中断" : "已结束";
  return <details className={styles.thinking} open={expanded} onToggle={event => setExpanded(event.currentTarget.open)} data-model-thinking>
    <summary><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg><span>模型思考</span><small>{status}</small></summary>
    <div className={styles.content}>{message.content || "正在接收思考内容…"}</div>
  </details>;
}
