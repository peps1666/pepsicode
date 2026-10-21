import type { ChatMessage } from "../stores/session";

export interface ToolActivity {
  message: ChatMessage;
  result?: ChatMessage;
}

// Correlate actual call IDs; old sessions retain a same-name FIFO fallback.
export function buildToolActivities(messages: ChatMessage[]): ToolActivity[] {
  const activities: ToolActivity[] = [];
  const pending = new Map<string, ToolActivity[]>();
  const calls = new Map<string, ToolActivity[]>();
  for (const message of messages) {
    const name = message.toolName || "tool";
    if (message.role === "tool_result") {
      const activity = message.callId ? calls.get(message.callId)?.shift() : pending.get(name)?.shift();
      if (activity) activity.result = message;
      else activities.push({ message });
    } else {
      const activity = { message };
      activities.push(activity);
      if (message.role === "tool_call") {
        const map = message.callId ? calls : pending;
        const key = message.callId || name;
        const queue = map.get(key) || [];
        queue.push(activity);
        map.set(key, queue);
      }
    }
  }
  return activities;
}

const labels: Record<string, string> = {
  read_file: "读取文件", write_file: "写入文件", edit_file: "编辑文件",
  patch_file: "应用补丁", multi_edit: "批量编辑", run_command: "运行命令", search_files: "搜索文件", grep_files: "搜索内容",
  list_files: "列出文件", file_tree: "浏览目录", web_fetch: "读取网页", web_search: "搜索网页",
  task: "运行子任务", load_skill: "加载技能", verification: "项目验证", test_runner: "运行测试",
};

export function describeTool(message: ChatMessage) {
  const name = message.toolName || "tool";
  const input = message.toolInput || {};
  const target = ["command", "path", "file_path", "pattern", "query", "url", "name", "description"]
    .map((key) => input[key])
    .find((value) => typeof value === "string" && value.trim());
  return {
    label: labels[name] || name.replace(/_/g, " ").replace(/^./, (char) => char.toUpperCase()),
    target: typeof target === "string" ? [target.replace(/\s+/g, " ").trim(), ...(name === "run_command" && Array.isArray(input.args) ? input.args.map(String) : [])].join(" ") : "",
  };
}
