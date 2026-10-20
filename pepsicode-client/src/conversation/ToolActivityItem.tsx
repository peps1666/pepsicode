import { useId } from "react";
import type { ToolActivity } from "./toolActivity";
import { describeTool } from "./toolActivity";
import styles from "./ToolActivityItem.module.css";

function ToolIcon({ name }: { name: string }) {
  const path = name === "run_command"
    ? <><path d="m4 5 4 3-4 3" /><path d="M10 12h3" /></>
    : /search|grep|glob/.test(name)
      ? <><circle cx="7" cy="7" r="4" /><path d="m10 10 3 3" /></>
      : /file|tree/.test(name)
        ? <><path d="M9 2H4v12h8V5Z" /><path d="M9 2v4h3M6 9h4M6 11h3" /></>
        : <><path d="m6 4-4 4 4 4m4-8 4 4-4 4" /></>;
  return <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{path}</svg>;
}

export default function ToolActivityItem({ activity, active }: { activity: ToolActivity; active: boolean }) {
  const detailsId = useId();
  const { message } = activity;
  if (message.role === "progress") {
    return <p className={styles.progress}>{message.content}</p>;
  }

  const result = activity.result || (message.role === "tool_result" ? message : undefined);
  const failed = Boolean(result?.isError);
  const running = !result && active;
  const status = failed ? "失败" : result ? "完成" : running ? "运行中" : "已停止";
  const { label, target } = describeTool(message);
  const input = message.toolInput ? JSON.stringify(message.toolInput, null, 2) : "";
  const output = result ? result.content || "没有返回输出。" : running ? "等待结果…" : "没有收到结果。";

  return (
    <details className={`${styles.activity} ${failed ? styles.failed : ""}`} open={failed || undefined}>
      <summary className={styles.activityHeader} aria-controls={detailsId}>
        <span className={styles.activityIcon}><ToolIcon name={message.toolName || ""} /></span>
        <span className={styles.label}>{label}</span>
        {target && <span className={styles.target} title={target}>{target}</span>}
        <span className={`${styles.status} ${running ? styles.running : ""}`}>
          {running ? <span className={styles.spinner} aria-hidden="true" /> : result ? <span aria-hidden="true">{failed ? "!" : "✓"}</span> : null}
          {status}
        </span>
        <svg className={styles.chevron} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
      </summary>
      <div className={styles.detailsBlock} id={detailsId}>
        <div className={styles.detailsHeading}>{message.toolName || "tool"}</div>
        {input && <><div className={styles.detailsLabel}>参数</div><pre className={styles.detailsContent}>{input}</pre></>}
        <div className={styles.detailsLabel}>{failed ? "错误输出" : "输出"}</div>
        <pre className={`${styles.detailsContent} ${failed ? styles.errorOutput : ""}`}>{output}</pre>
      </div>
    </details>
  );
}
