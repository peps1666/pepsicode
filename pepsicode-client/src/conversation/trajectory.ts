import type { ChatMessage, VerificationReport } from "../stores/session";
import { buildToolActivities, describeTool } from "./toolActivity";

export type TraceStatus = "completed" | "running" | "failed" | "blocked" | "cancelled" | "not_run" | "retry";
export interface TraceRow {
  id: string; turn: number; kind: "user" | "assistant" | "tool" | "progress" | "retry" | "verification";
  label: string; summary: string; output: string; status: TraceStatus;
  input?: Record<string, unknown>; callId?: string; startedAt?: number; durationMs?: number; exitCode?: number;
  retry?: ChatMessage["retry"];
}

export const traceStatusLabels: Record<TraceStatus, string> = {
  completed: "已完成", running: "进行中", failed: "失败", blocked: "受阻", cancelled: "已中断", not_run: "未运行", retry: "重试",
};

export function verificationSummary(report: VerificationReport): string {
  const reasons: Record<string, string> = { no_changes: "本轮没有需要验证的文件修改", no_checks: "项目未发现自动检查命令", no_tests: "检查没有收集到测试", disabled: "项目未开启自动验证", read_only: "只读或计划模式下未执行检查", awaiting_user: "等待补充信息" };
  if (report.reason_code && reasons[report.reason_code]) return reasons[report.reason_code];
  if (report.reason.includes("No verification commands found")) return reasons.no_checks;
  if (report.reason.includes("Agent turn failed before verification")) return "请求未完成，未执行验证";
  if (report.status === "passed") return "项目检查通过";
  if (report.status === "failed") return "项目检查未通过，展开查看输出";
  if (report.status === "cancelled") return "验证已停止";
  return report.reason || "展开查看验证详情";
}

export function formatDuration(duration?: number): string {
  if (duration === undefined || !Number.isFinite(duration)) return "—";
  return duration < 1000 ? `${Math.max(0, Math.round(duration))} ms` : `${(duration / 1000).toFixed(1)} s`;
}

export function buildTrajectory(messages: ChatMessage[], running: boolean, verification: VerificationReport | null = null): TraceRow[] {
  const turns: ChatMessage[][] = [];
  for (const message of messages) {
    if (message.role === "system") continue;
    if (message.role === "user" || !turns.length) turns.push([]);
    turns[turns.length - 1].push(message);
  }
  return turns.flatMap((turnMessages, turnIndex) => {
    const turn = turnIndex + 1;
    const active = running && turn === turns.length;
    const activities = buildToolActivities(turnMessages.filter(message => message.role === "tool_call" || message.role === "tool_result"));
    const byMessage = new Map(activities.map(activity => [activity.message, activity]));
    const rows: TraceRow[] = [];
    turnMessages.forEach((message, index) => {
      if (message.role === "tool_result" && !byMessage.has(message)) return;
      const activity = byMessage.get(message);
      const id = `turn-${turn}-step-${index}`;
      if (activity) {
        const result = activity.result || (message.role === "tool_result" ? message : undefined);
        const tool = describeTool(message);
        const status: TraceStatus = result?.toolStatus === "cancelled" ? "cancelled" : result?.toolStatus === "blocked" ? "blocked" : result?.isError ? "failed" : result ? "completed" : active ? "running" : "cancelled";
        rows.push({ id: message.callId ? `turn-${turn}-tool-${message.callId}` : id, turn, kind: "tool", label: tool.label, summary: tool.target || message.toolName || "工具调用", input: message.toolInput,
          output: result?.content || (result ? "没有返回输出。" : active ? "等待工具返回结果…" : "执行已结束，未收到结果。"), status,
          callId: message.callId || result?.callId, startedAt: message.startedAt || result?.startedAt, durationMs: result?.durationMs, exitCode: result?.exitCode });
      } else {
        const retry = message.activityKind === "retry";
        const kind = retry ? "retry" : message.role === "progress" ? "progress" : message.role === "user" ? "user" : "assistant";
        rows.push({ id, turn, kind, label: retry ? "模型请求重试" : kind === "progress" ? "执行进度" : kind === "user" ? "用户输入" : "模型回复",
          summary: message.content.replace(/\s+/g, " ").trim() || "正在生成回复…", output: message.content, retry: message.retry, startedAt: message.retry?.started_at,
          status: message.isError ? "failed" : message.isStreaming ? "running" : retry ? "retry" : "completed" });
      }
    });
    const saved = [...turnMessages].reverse().find(message => message.verification)?.verification;
    const report = turn === turns.length && verification ? verification : saved;
    if (report) {
      const status: TraceStatus = report.status === "passed" ? "completed" : ["failed", "blocked", "cancelled"].includes(report.status) ? report.status as TraceStatus : "not_run";
      rows.push({ id: `turn-${turn}-verification`, turn, kind: "verification", label: "项目验证", summary: verificationSummary(report), output: [verificationSummary(report), report.details].filter(Boolean).join("\n\n"), status });
    }
    return rows;
  });
}
