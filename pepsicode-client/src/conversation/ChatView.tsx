import { useEffect, useRef, useState, useCallback } from "react";
import { useSessionStore, type ChatMessage } from "../stores/session";
import MessageItem from "./MessageItem";
import Composer from "./Composer";
import styles from "./ChatView.module.css";

export default function ChatView({ onSend }: { onSend: (input: string) => void }) {
  const { messages, isRunning, cwd, model, planMode } = useSessionStore();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, autoScroll]);

  const handleScroll = useCallback(() => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    const atBottom = scrollHeight - scrollTop - clientHeight < 50;
    setAutoScroll(atBottom);
  }, []);

  return (
    <div className={styles.container}>
      <header className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.modelName}>{model}</span>
          {planMode && <span className={styles.planBadge}>PLAN MODE</span>}
        </div>
        <div className={styles.headerRight}>
          <span className={styles.cwd} title={cwd}>{cwd}</span>
        </div>
      </header>
      <div className={styles.messageList} ref={scrollRef} onScroll={handleScroll}>
        {messages.length === 0 ? (
          <div className={styles.emptyState}>
            <h2>Pepsicode</h2>
            <p>Ask anything, or type / for commands</p>
          </div>
        ) : (
          messages.map((msg) => <MessageItem key={msg.id} message={msg} />)
        )}
      </div>
      <Composer onSend={onSend} disabled={isRunning} />
    </div>
  );
}
