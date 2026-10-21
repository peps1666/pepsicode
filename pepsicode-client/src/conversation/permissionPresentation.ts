import type { PermissionRequest } from "../stores/session";

export function permissionPresentation(request: PermissionRequest) {
  const names: Record<string, { identity: string; title: string }> = {
    command: { identity: "终端", title: "允许 Pepsicode 运行这条命令吗？" },
    path: { identity: "文件访问", title: "允许访问项目外的目录吗？" },
    edit: { identity: "编辑文件", title: "允许 Pepsicode 修改这个文件吗？" },
    plan: { identity: "执行计划", title: "按这份计划开始执行吗？" },
    mcp: { identity: "外部工具", title: "允许调用这个外部工具吗？" },
  };
  const field = (name: string) => request.details.find(line => line.startsWith(`${name}: `))?.slice(name.length + 2) || "";
  const reason = field("reason");
  return {
    ...(names[request.kind] || { identity: "权限请求", title: request.summary || "这项操作需要你的确认" }),
    preview: field("command") || field("target") || field("tool") || request.scope,
    cwd: field("cwd"),
    reason: /^python can execute arbitrary local code/i.test(reason) ? "这条命令会执行 Python 代码，需要你的确认。"
      : /^Unknown command .* is not in the built-in read-only\/development set/.test(reason) ? "该程序不在内置命令列表中，需要你的确认。" : reason,
  };
}

export function permissionChoice(request: PermissionRequest, choice: PermissionRequest["choices"][number]) {
  const subject = request.kind === "command" ? "此命令" : request.kind === "path" ? "此目录" : request.kind === "edit" ? "此文件" : request.kind === "mcp" ? "此工具" : "此操作";
  const labels: Record<string, [string, string]> = {
    allow_once: [request.kind === "plan" ? "批准计划" : "本次会话允许", request.kind === "plan" ? "批准这份执行计划" : "当前会话内记住相同请求的允许决定"],
    deny_once: ["拒绝", "当前会话内记住相同请求的拒绝决定"],
    allow_always: [`始终允许${subject}`, "保存到允许列表，后续匹配的请求无需再次确认"],
    deny_always: [`始终拒绝${subject}`, "保存到拒绝列表，后续匹配的请求自动拒绝"],
    allow_turn: ["本轮允许此文件", "仅在当前这一轮任务中生效"],
    allow_all_turn: ["本轮允许所有文件编辑", "仅在当前这一轮任务中生效"],
    deny_with_feedback: ["拒绝并提出修改意见", "将你的意见交给模型调整下一步操作"],
  };
  const [label, description] = labels[choice.decision] || [choice.label, ""];
  return { label, description };
}
