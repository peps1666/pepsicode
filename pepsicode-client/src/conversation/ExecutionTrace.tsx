import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { ChatMessage, TaskReport, VerificationReport } from "../stores/session";
import { buildTrajectory, formatDuration, traceStatusLabels, type TraceRow } from "./trajectory";
import Icon from "../components/Icon";
import Button from "../components/Button";
import styles from "./ExecutionTrace.module.css";

export default function ExecutionTrace({ messages, running, verification, taskReport, canContinue, onContinue }: {
  messages: ChatMessage[]; running: boolean; verification: VerificationReport | null;
  taskReport?: TaskReport | null;
  canContinue: boolean; onContinue: (row: TraceRow) => void;
}) {
  const rows = useMemo(() => buildTrajectory(messages, running, verification), [messages, running, verification]);
  const [query, setQuery] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [limit, setLimit] = useState(200);
  const [selectedId, setSelectedId] = useState<string | null>(() => [...rows].reverse().find(row => row.status === "failed" || row.status === "blocked")?.id || rows[rows.length - 1]?.id || null);
  const selected = rows.find(row => row.id === selectedId);
  const ledger = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const task = taskReport !== undefined ? taskReport : [...messages].reverse().find(message => message.task)?.task;
  const latestUser = [...messages].reverse().find(message => message.role === "user");
  const taskLabels: Record<string, string> = { complete: "已完成", answered: "已回答", active: "处理中", incomplete: "未完成", blocked: "需要处理阻碍", waiting_user: "等待回复", cancelled: "已停止", unknown: "待核对", budget_exhausted: "预算已用尽" };
  const failures = rows.filter(row => row.status === "failed" || row.status === "blocked").length;
  const needle = query.trim().toLocaleLowerCase();
  const filtered = rows.filter(row => (!errorsOnly || ["failed", "blocked", "retry", "cancelled"].includes(row.status)) &&
    (!needle || [row.label, row.summary, row.output, row.callId].filter(Boolean).join(" ").toLocaleLowerCase().includes(needle)));
  const visible = filtered.slice(-limit);
  const indices = new Map(rows.map((row, index) => [row.id, index + 1]));
  useEffect(() => {
    if (follow.current && ledger.current) ledger.current.scrollTop = ledger.current.scrollHeight;
  }, [rows.length]);

  return <section className={styles.root} aria-label="执行轨迹">
    {(task || (running && latestUser)) && <div className={styles.taskStatus} role="status"><strong>任务：{running ? "处理中" : taskLabels[task?.status || ""] || "待验收"}</strong><span>{(running ? latestUser?.content : task?.objective)?.slice(0, 300)}</span>{!running && task && task.criteria.length > 0 && <small>验收 {task.evidence.filter(item => item.status === "satisfied").length}/{task.criteria.length}</small>}</div>}
    <div className={styles.toolbar}>
      <div><strong>执行轨迹</strong><span>{rows.filter(row => row.kind === "tool").length} 次工具调用{failures > 0 ? ` · ${failures} 条异常` : ""}</span></div>
      <div className={styles.filters}>
        <input aria-label="搜索执行轨迹" placeholder="搜索工具、文件或错误…" value={query} onChange={event => { setQuery(event.target.value); setLimit(200); }} />
        <button type="button" aria-pressed={errorsOnly} onClick={() => { setErrorsOnly(value => !value); setLimit(200); }}>仅看异常</button>
      </div>
    </div>
    <div className={`${styles.layout} ${selected ? styles.withInspector : ""}`}>
      <div className={styles.ledger} ref={ledger} onScroll={event => {
        const node = event.currentTarget;
        follow.current = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
      }}>
        {filtered.length > limit && <Button variant="ghost" className={styles.loadMore} onClick={() => { follow.current = false; setLimit(value => value + 200); }}>加载更早的记录（还有 {filtered.length - limit} 条）</Button>}
        {visible.length ? <table className={styles.table} aria-label="按轮次排列的执行记录">
          <thead><tr><th>步骤</th><th>事件</th><th>内容</th><th>状态</th><th>耗时</th></tr></thead>
          <tbody>{visible.map((row, index) => <Fragment key={row.id}>
            {(index === 0 || visible[index - 1].turn !== row.turn) && <tr className={styles.turn}><td colSpan={5}>第 {row.turn} 轮</td></tr>}
            <tr className={styles.row} data-selected={selectedId === row.id || undefined} data-status={row.status} data-call-id={row.callId} onClick={() => setSelectedId(row.id)}>
              <td className={styles.index}>{String(indices.get(row.id)).padStart(2, "0")}</td>
              <td><button type="button" className={styles.event} aria-label={`查看步骤 ${indices.get(row.id)}：${row.label}`} aria-pressed={selectedId === row.id} onClick={() => setSelectedId(row.id)}><span className={styles.dot} />{row.label}</button></td>
              <td className={styles.summary} title={row.summary}>{row.summary}</td>
              <td><span className={styles.status}>{traceStatusLabels[row.status]}</span></td>
              <td className={styles.duration}>{formatDuration(row.durationMs)}</td>
            </tr>
          </Fragment>)}</tbody>
        </table> : <div className={styles.empty}>{rows.length ? "没有匹配的记录" : "发送一条消息后，模型与工具的执行过程会记录在这里。"}</div>}
      </div>
      {selected && <aside className={styles.inspector} aria-label="执行步骤详情">
        <header><div><small>第 {selected.turn} 轮 · 步骤 {indices.get(selected.id)}</small><h3>{selected.label}</h3></div><Button variant="ghost" aria-label="关闭步骤详情" className={styles.close} onClick={() => setSelectedId(null)}><Icon name="close" size={16} /></Button></header>
        <dl className={styles.metadata}><dt>状态</dt><dd data-status={selected.status}>{traceStatusLabels[selected.status]}</dd><dt>耗时</dt><dd>{formatDuration(selected.durationMs)}</dd>
          {selected.callId && <><dt>调用 ID</dt><dd>{selected.callId}</dd></>}
          {selected.startedAt !== undefined && <><dt>开始时间</dt><dd>{new Date(selected.startedAt).toLocaleTimeString("zh-CN", { hour12: false })}</dd></>}
          {selected.exitCode !== undefined && <><dt>退出码</dt><dd>{selected.exitCode}</dd></>}
          {selected.retry && <><dt>重试次数</dt><dd>{selected.retry.attempt} / {selected.retry.limit}</dd><dt>重试原因</dt><dd>{selected.retry.reason}</dd><dt>等待间隔</dt><dd>{formatDuration(selected.retry.delay_ms)}</dd></>}
        </dl>
        {selected.input && <section><h4>调用参数</h4><pre>{JSON.stringify(selected.input, null, 2)}</pre></section>}
        <section><h4>{selected.status === "failed" || selected.status === "blocked" ? "失败原因与输出" : selected.kind === "verification" ? "验证记录" : selected.kind === "tool" ? "执行输出" : "内容"}</h4><pre>{selected.output || "等待内容…"}</pre></section>
        {(["failed", "blocked", "cancelled"].includes(selected.status) && selected.kind !== "verification") && <div className={styles.recovery}>
          <p>基于当前记录继续分析失败原因，由模型决定下一步操作。</p>
          <Button variant="primary" disabled={!canContinue} onClick={() => onContinue(selected)}>继续处理</Button>
        </div>}
        {selected.durationMs === undefined && <p className={styles.note}>未记录的耗时显示为「—」。旧会话仍可查看已有参数和结果。</p>}
      </aside>}
    </div>
  </section>;
}
