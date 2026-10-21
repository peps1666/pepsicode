import { useEffect, useRef, useState } from "react";
import { useSessionStore, type PermissionRequest } from "../stores/session";
import Button from "../components/Button";
import Icon from "../components/Icon";
import { permissionChoice, permissionPresentation } from "./permissionPresentation";
import styles from "./PermissionPrompt.module.css";

export default function PermissionPrompt() {
  const { pendingPermission, approvePermission } = useSessionStore();
  return pendingPermission ? <PermissionCard key={pendingPermission.prompt_id} request={pendingPermission} approve={approvePermission} /> : null;
}

export function PermissionCard({ request, approve }: {
  request: PermissionRequest; approve: (decision: string, feedback?: string) => Promise<void>;
}) {
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const card = useRef<HTMLElement>(null);
  const presentation = permissionPresentation(request);
  const once = request.choices.find(choice => choice.decision === "allow_once");
  const deny = request.choices.find(choice => choice.decision === "deny_once");
  const others = request.choices.filter(choice => choice !== once && choice !== deny);
  const longPreview = presentation.preview.length > 180 || presentation.preview.split("\n").length > 3;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    card.current?.focus({ preventScroll: true });
    return () => {
      if (document.activeElement === document.body && previous?.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);

  async function choose(decision: string, guidance?: string) {
    if (inFlight.current) return;
    if (decision === "deny_with_feedback" && guidance === undefined) { setShowFeedback(true); return; }
    inFlight.current = true;
    setSending(true); setError("");
    try { await approve(decision, guidance); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { inFlight.current = false; setSending(false); }
  }

  return <section ref={card} tabIndex={-1} role="region" aria-label="操作权限确认" aria-labelledby={`approval-${request.prompt_id}`} aria-busy={sending} className={styles.card}>
    <header className={styles.header}><span className={styles.identity}><Icon name={request.kind === "command" ? "terminal" : request.kind === "edit" ? "compose" : "shield"} size={15} />{presentation.identity}</span><span className={styles.waiting}>{sending ? "正在提交…" : "等待确认"}</span></header>
    <div className={styles.body}>
      <h3 id={`approval-${request.prompt_id}`}>{presentation.title}</h3>
      {presentation.reason && <p className={styles.reason}>{presentation.reason}</p>}
      {presentation.preview && <div className={styles.preview}>
        <pre className={!expanded && longPreview ? styles.clamped : undefined}>{presentation.preview}</pre>
        {longPreview && <button type="button" className={styles.expand} aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? "收起命令" : "展开完整内容"}</button>}
      </div>}
      {presentation.cwd && <p className={styles.cwd} title={presentation.cwd}><Icon name="folder" size={12} />{presentation.cwd}</p>}
      <details className={styles.details}><summary>查看请求详情</summary><pre>{[request.summary, ...request.details, request.scope ? `授权范围: ${request.scope}` : ""].filter(Boolean).join("\n")}</pre></details>
      {showOptions && !showFeedback && <div className={styles.options} role="group" aria-label="其他授权方式">
        {others.map(choice => {
          const { label, description } = permissionChoice(request, choice);
          return <button type="button" key={choice.key} disabled={sending} onClick={() => void choose(choice.decision)}><span>{label}</span><small>{description}</small></button>;
        })}
      </div>}
      {showFeedback && <div className={styles.feedback}>
        <label htmlFor={`feedback-${request.prompt_id}`}>修改意见</label>
        <textarea id={`feedback-${request.prompt_id}`} placeholder="告诉模型如何调整下一步操作" value={feedback} onChange={event => setFeedback(event.target.value)} disabled={sending} autoFocus rows={2} />
      </div>}
      {error && <p className={styles.error} role="alert">提交失败：{error}。请重试。</p>}
    </div>
    <footer className={styles.actions}>
      {showFeedback ? <>
        <Button variant="ghost" disabled={sending} onClick={() => setShowFeedback(false)}>返回</Button>
        <Button variant="primary" disabled={sending || !feedback.trim()} onClick={() => void choose("deny_with_feedback", feedback.trim())}>发送意见并拒绝</Button>
      </> : <>
        {others.length > 0 && <Button variant="ghost" className={styles.more} disabled={sending} aria-expanded={showOptions} onClick={() => setShowOptions(value => !value)}>更多选项<Icon name="chevron" size={12} /></Button>}
        {deny && <Button disabled={sending} title={permissionChoice(request, deny).description} onClick={() => void choose(deny.decision)}>{permissionChoice(request, deny).label}</Button>}
        {once && <Button variant="primary" disabled={sending} title={permissionChoice(request, once).description} onClick={() => void choose(once.decision)}>{permissionChoice(request, once).label}</Button>}
      </>}
    </footer>
  </section>;
}
