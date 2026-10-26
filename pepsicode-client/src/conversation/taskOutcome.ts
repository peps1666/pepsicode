import type { TaskReport } from "../stores/session";

export function hasCurrentAcceptance(task: TaskReport): boolean {
  return Boolean(task.acceptance?.valid && task.acceptance.task_revision === task.revision && task.acceptance.acceptance_run_id && task.acceptance.workspace?.digest);
}

export function satisfiedCount(task: TaskReport): number {
  if (!hasCurrentAcceptance(task)) return 0;
  const proof = task.acceptance!;
  return task.evidence.filter(item => item.status === "satisfied" && item.task_revision === task.revision &&
    item.acceptance_run_id === proof.acceptance_run_id && item.workspace_digest === proof.workspace.digest).length;
}

export function taskOutcome(task: TaskReport): string {
  if (task.status === "complete") {
    if (!hasCurrentAcceptance(task)) return "需要重新核对";
    return task.assurance === "behavior_checked" ? "声明的行为检查已通过" : "产物已检查，行为未验证";
  }
  const labels: Record<string, string> = { answered: "已回答", active: "处理中", incomplete: "未完成", blocked: "需要处理阻碍",
    waiting_user: "等待回复", cancelled: "已停止", unknown: "待核对", budget_exhausted: "预算已用尽" };
  return labels[task.status] || "待验收";
}
