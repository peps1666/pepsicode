import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useSessionStore, type AgentSnapshot, type ChatMessage, type TaskReport, type VerificationReport } from "../stores/session";
import { buildTrajectory, formatDuration, traceStatusLabels, type TraceRow } from "./trajectory";
import Icon from "../components/Icon";
import Button from "../components/Button";
import styles from "./ExecutionTrace.module.css";
import { hasCurrentAcceptance, satisfiedCount, taskOutcome } from "./taskOutcome";

const agentStages: Record<string, string> = {
  queued: "排队中", stop_requested: "正在停止",
  starting: "正在启动", idle: "等待下一步", running: "运行中", completed: "已返回",
  waiting_user: "等待回复", awaiting_continuation: "等待继续", waiting_approval: "等待审批",
  pending_verification: "结果待核验", reported_blocked: "Worker 报告受阻", reported_failed: "Worker 报告失败",
  verified: "父任务验收通过",
  reported_dropped: "Worker 报告放弃", stale_result: "旧版本结果", recovery_pending: "中断待核对",
  unknown: "结果未知", cancelled: "已停止", timed_out: "超时", failed: "失败", blocked: "受阻",
  budget_exhausted: "达到预算或执行限制",
};

function AgentCards({ canReply, running, onSelectCall }: { canReply: boolean; running: boolean; onSelectCall: (id: string) => void }) {
  const live = useSessionStore(state => state.agents);
  const fetchAgents = useSessionStore(state => state.fetchAgents);
  const stopWorker = useSessionStore(state => state.stopWorker);
  const runTurn = useSessionStore(state => state.runTurn);
  const [page, setPage] = useState<AgentSnapshot | null>(null);
  const [error, setError] = useState("");
  const view = page?.session_id === live?.session_id && page?.sequence === live?.sequence ? page : live;
  if (!view || (!view.records.length && !view.persistence_error)) return null;
  return <section className={styles.agentCards} aria-label="Agent 状态">
    <strong>Agent 状态</strong>
    {view.persistence_error && <p role="alert">追踪状态保存失败：{view.persistence_error}</p>}
    {error && <p role="alert">{error}</p>}
    {view.records.map(agent => <details key={agent.trace_id} className={styles.agentCard}>
      <summary>
        <strong>{agent.name}</strong> · {agentStages[agent.stage] || agent.stage}
        {agent.historical && " · 历史分支"} · {formatDuration(agent.elapsed_seconds * 1000)}
        <small>{agent.task}</small>
        {agent.activities.map(activity => <small key={activity.id}>{activity.summary}</small>)}
      </summary>
      <small>执行 {agent.trace_id} · 负责人 {agent.parent_id || "当前会话"}</small>
      {running && agent.can_stop && <button type="button" onClick={() => {
        void stopWorker(agent.trace_id).catch(error => setError(String(error)));
      }}>停止 Worker</button>}
      {view === live && <button type="button" onClick={() => {
        void fetchAgents(0, agent.trace_id).then(setPage).catch(error => setError(String(error)));
      }}>查看完整记录</button>}
      {agent.parent_call_id && <button type="button" onClick={() => onSelectCall(agent.parent_call_id!)}>查看发起步骤</button>}
      {agent.report && <p>最近报告{agent.stale ? "（旧版本）" : ""} · {agent.report.summary}</p>}
      {agent.result_summary && agent.result_summary !== agent.report?.summary && <p>{agent.result_summary}</p>}
      {agent.end_reason && <p>{agent.end_reason}</p>}
      {agent.acceptance.total > 0 && <small>父任务验收 {agent.acceptance.passed}/{agent.acceptance.total}</small>}
      {agent.questions.map(question => <div key={question.id}>
        <p>{question.status === "answered" ? "已答" : question.required ? "必答" : "可选"} · {question.question}</p>
        {question.actionable && (canReply || (running && agent.can_reply_running)) && <form onSubmit={event => {
          event.preventDefault();
          const form = event.currentTarget;
          const reply = String(new FormData(form).get("reply") || "").trim();
          if (reply) void runTurn(reply, "steer", [question.id]).then(() => form.reset()).catch(error => setError(String(error)));
        }}>
          <input name="reply" aria-label="回复 Agent" required />
          <button type="submit">回复</button>
        </form>}
      </div>)}
      {agent.recent_tools.map(tool => <details key={tool.id}>
        <summary>{tool.summary} · {tool.status} · {formatDuration(tool.duration_ms)}</summary>
        {tool.input_preview && <pre aria-label="调用参数预览">{tool.input_preview}</pre>}
        <pre>{tool.output}</pre>
      </details>)}
    </details>)}
    {view !== live && <button type="button" onClick={() => setPage(null)}>回到最新执行</button>}
    {view.next_offset !== null && <button type="button" onClick={() => {
      void fetchAgents(view.next_offset!).then(setPage).catch(error => setError(String(error)));
    }}>查看更早的执行</button>}
  </section>;
}

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
  const validAcceptance = task ? hasCurrentAcceptance(task) : false;
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
    {(task || (running && latestUser)) && <div className={styles.taskStatus} role="status">
      <strong>任务：{running ? "处理中" : task ? taskOutcome(task) : "待验收"}</strong>
      <span>{(running ? latestUser?.content : task?.objective)?.slice(0, 300)}</span>
      {!running && task?.instructions?.length ? <small>最新补充：{task.instructions.at(-1)?.slice(0, 300)}</small> : null}
      {!running && task && task.criteria.length > 0 && <small>验收 {satisfiedCount(task)}/{task.criteria.length}{!validAcceptance && " · 待复核"}</small>}
      {!running && task?.acceptance && <details><summary>检查依据</summary>
        <small>任务版本 {task.revision} · 状态 {task.acceptance.workspace?.digest?.slice(0, 12) || "未记录"} · 验收 {task.acceptance.acceptance_run_id?.slice(0, 12) || "未记录"}</small>
        <ul>{task.criteria.map(criterion => <li key={criterion.id}>{criterion.purpose || criterion.id}：{criterion.kind === "command" ? "执行检查" : criterion.kind === "file_contains" ? "文本匹配" : "文件存在"} · {(criterion.path || criterion.argv?.join(" ") || "").slice(0, 1000)}</li>)}</ul>
      </details>}
    </div>}
    <AgentCards canReply={canContinue} running={running} onSelectCall={id => {
      const row = rows.find(item => item.callId === id);
      if (row) setSelectedId(row.id);
    }} />
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
