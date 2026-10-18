import { useEffect, useRef, useState, useCallback } from "react";
import { useSessionStore, type ChatMessage } from "../stores/session";
import MessageItem from "./MessageItem";
import Composer from "./Composer";
import ThinkingGroup, { isThinkingMessage } from "./ThinkingGroup";
import ModelSettings, { effortLabels } from "./ModelSettings";
import styles from "./ChatView.module.css";

type TimelineBlock =
  | { kind: "message"; message: ChatMessage }
  | { kind: "thinking"; messages: ChatMessage[]; endIndex: number };

function buildTimeline(messages: ChatMessage[]): TimelineBlock[] {
  const blocks: TimelineBlock[] = [];
  let thinking: ChatMessage[] = [];

  const flushThinking = (endIndex: number) => {
    if (thinking.length === 0) return;
    blocks.push({ kind: "thinking", messages: thinking, endIndex });
    thinking = [];
  };

  messages.forEach((message, index) => {
    if (isThinkingMessage(message)) {
      thinking.push(message);
      return;
    }
    flushThinking(index - 1);
    blocks.push({ kind: "message", message });
  });
  flushThinking(messages.length - 1);
  return blocks;
}

export default function ChatView({ onSend }: { onSend: (input: string) => void }) {
  const { messages, isRunning, cwd, model, models, reasoningEffort, isMock, modelUpdating, sessionId, planMode } = useSessionStore();
  const modelName = models.find((item) => item.id === model)?.name || model;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const timeline = buildTimeline(messages);
  let lastUserIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "user") {
      lastUserIndex = index;
      break;
    }
  }
  const currentTurnHasAnswer = messages.slice(lastUserIndex + 1).some(
    (message) => message.role === "assistant" && !message.isStreaming && Boolean(message.content)
  );

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
          <button className={styles.modelName} type="button" onClick={() => setSettingsOpen(true)}
            disabled={isRunning || modelUpdating || !sessionId}
            title={isMock ? "添加厂商并配置 API" : "选择模型与思考深度"}
            aria-haspopup="dialog">
            {isMock ? "配置模型与服务商" : `${modelName} · ${effortLabels[reasoningEffort] || reasoningEffort}`} ▾
          </button>
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
          <div className={styles.column}>
            {timeline.map((block) => block.kind === "message" ? (
              <MessageItem key={block.message.id} message={block.message} />
            ) : (
              <ThinkingGroup
                key={`thinking-${block.messages[0].id}`}
                messages={block.messages}
                autoExpanded={isRunning && !currentTurnHasAnswer && block.endIndex > lastUserIndex}
              />
            ))}
          </div>
        )}
      </div>
      <Composer onSend={onSend} disabled={isRunning || modelUpdating} />
      {settingsOpen && <ModelSettings onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
