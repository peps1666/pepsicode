import { useEffect, useId, useRef, useState } from "react";
import type { ChatMessage } from "../stores/session";
import ToolActivityItem from "./ToolActivityItem";
import { buildToolActivities } from "./toolActivity";
import styles from "./ThinkingGroup.module.css";

export function isThinkingMessage(message: ChatMessage): boolean {
  return message.role === "progress" || message.role === "tool_call" || message.role === "tool_result";
}

interface ThinkingGroupProps {
  messages: ChatMessage[];
  autoExpanded: boolean;
}

export default function ThinkingGroup({ messages, autoExpanded }: ThinkingGroupProps) {
  const failures = messages.filter((message) => message.role === "tool_result" && message.isError).length;
  const [expanded, setExpanded] = useState(autoExpanded || failures > 0);
  const wasAutoExpanded = useRef(autoExpanded);
  const contentId = useId();
  const activities = buildToolActivities(messages);

  useEffect(() => {
    if (autoExpanded) {
      setExpanded(true);
    } else if (wasAutoExpanded.current) {
      setExpanded(failures > 0);
    }
    wasAutoExpanded.current = autoExpanded;
  }, [autoExpanded, failures]);

  const toolCalls = messages.filter((message) => message.role === "tool_call").length;
  const summary = [
    toolCalls > 0 ? `${toolCalls} ${toolCalls === 1 ? "tool" : "tools"}` : null,
    failures > 0 ? `${failures} failed` : null,
  ].filter(Boolean).join(" · ");

  return (
    <section className={styles.container}>
      <button
        type="button"
        className={styles.header}
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-controls={contentId}
      >
        <span className={`${styles.indicator} ${autoExpanded ? styles.indicatorActive : ""}`} />
        <span className={styles.title}>{autoExpanded ? "Working…" : "Activity"}</span>
        {summary && <span className={`${styles.summary} ${failures ? styles.hasFailures : ""}`}>{summary}</span>}
        <svg className={`${styles.chevron} ${expanded ? styles.chevronOpen : ""}`} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
      </button>
      {expanded && (
        <div className={styles.content} id={contentId}>
          {activities.map((activity) => <ToolActivityItem key={activity.message.id} activity={activity} active={autoExpanded} />)}
        </div>
      )}
    </section>
  );
}
