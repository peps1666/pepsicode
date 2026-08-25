import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "../stores/session";
import ToolActivityItem from "./ToolActivityItem";
import styles from "./ThinkingGroup.module.css";

export function isThinkingMessage(message: ChatMessage): boolean {
  return message.role === "progress" || message.role === "tool_call" || message.role === "tool_result";
}

interface ThinkingGroupProps {
  messages: ChatMessage[];
  autoExpanded: boolean;
}

export default function ThinkingGroup({ messages, autoExpanded }: ThinkingGroupProps) {
  const [expanded, setExpanded] = useState(autoExpanded);
  const wasAutoExpanded = useRef(autoExpanded);

  useEffect(() => {
    if (autoExpanded) {
      setExpanded(true);
    } else if (wasAutoExpanded.current) {
      setExpanded(false);
    }
    wasAutoExpanded.current = autoExpanded;
  }, [autoExpanded]);

  const toolCalls = messages.filter((message) => message.role === "tool_call").length;
  const failures = messages.filter((message) => message.role === "tool_result" && message.isError).length;
  const summary = [
    toolCalls > 0 ? `${toolCalls} tool ${toolCalls === 1 ? "call" : "calls"}` : null,
    failures > 0 ? `${failures} failed` : null,
  ].filter(Boolean).join(" · ");

  return (
    <section className={styles.container}>
      <button
        type="button"
        className={styles.header}
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className={`${styles.chevron} ${expanded ? styles.chevronOpen : ""}`}>›</span>
        <span className={`${styles.indicator} ${autoExpanded ? styles.indicatorActive : ""}`} />
        <span className={styles.title}>{autoExpanded ? "Thinking…" : "Thought process"}</span>
        {summary && <span className={styles.summary}>{summary}</span>}
      </button>
      {expanded && (
        <div className={styles.content}>
          {messages.map((message) => <ToolActivityItem key={message.id} message={message} />)}
        </div>
      )}
    </section>
  );
}
