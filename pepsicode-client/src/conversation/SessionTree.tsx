import { useEffect } from "react";
import { useSessionStore } from "../stores/session";
import styles from "./SessionTree.module.css";

export default function SessionTree({ onClose }: { onClose: () => void }) {
  const { tree, refreshTree, checkoutEntry, forkSession, cloneSession, isRunning, branchUpdating, branchError } = useSessionStore();
  const disabled = isRunning || branchUpdating;
  useEffect(() => { void refreshTree(); }, [refreshTree]);
  const run = (action: () => Promise<void>) => { void action().catch(() => {}); };
  return <section className={styles.panel} aria-label="对话分支">
    <div className={styles.header}><b>对话分支</b><button onClick={onClose}>关闭</button></div>
    <p>各分支共用当前工作目录，切换不会撤销代码。选择用户消息会返回该消息前，并将原文放入编辑框。</p>
    <button disabled={disabled} onClick={() => run(cloneSession)}>复制当前路线为新会话</button>
    {tree?.parent_session_id && <small>来自会话 {tree.parent_session_id}</small>}
    {branchError && <p role="alert">{branchError}</p>}
    <ul>{tree?.nodes.map((node) => <li key={node.id} style={{ marginLeft: Math.min(node.depth, 5) * 10 }}>
      <span title={node.id}>{node.active ? "● " : "○ "}{node.role === "user" ? "你" : "助手"} · {node.id.slice(0, 8)}</span>
      <div className={styles.preview}>{node.preview}</div>
      <div className={styles.actions}>
        <button disabled={disabled} onClick={() => run(() => checkoutEntry(node.id))}>{node.role === "user" ? "从此修改" : "从此继续"}</button>
        {node.role === "user" && <button disabled={disabled} onClick={() => run(() => forkSession(node.id))}>分叉为新会话</button>}
      </div>
    </li>)}</ul>
    {!tree?.nodes.length && <p>发送一条消息后即可创建分支。</p>}
  </section>;
}
