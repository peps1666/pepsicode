import type { ChatMessage } from "../stores/session";

export interface ToolActivity {
  message: ChatMessage;
  result?: ChatMessage;
}

// The protocol has no call IDs: pair same-name results in arrival order.
export function buildToolActivities(messages: ChatMessage[]): ToolActivity[] {
  const activities: ToolActivity[] = [];
  const pending = new Map<string, ToolActivity[]>();
  for (const message of messages) {
    const name = message.toolName || "tool";
    if (message.role === "tool_result") {
      const activity = pending.get(name)?.shift();
      if (activity) activity.result = message;
      else activities.push({ message });
    } else {
      const activity = { message };
      activities.push(activity);
      if (message.role === "tool_call") {
        const queue = pending.get(name) || [];
        queue.push(activity);
        pending.set(name, queue);
      }
    }
  }
  return activities;
}

const labels: Record<string, string> = {
  read_file: "Read file", write_file: "Write file", edit_file: "Edit file",
  patch_file: "Patch file", run_command: "Run command", search_files: "Search files",
  list_files: "List files", file_tree: "Explore files", web_fetch: "Fetch page",
  task: "Run agent", load_skill: "Load skill",
};

export function describeTool(message: ChatMessage) {
  const name = message.toolName || "tool";
  const input = message.toolInput || {};
  const target = ["command", "path", "file_path", "pattern", "query", "url", "name", "description"]
    .map((key) => input[key])
    .find((value) => typeof value === "string" && value.trim());
  return {
    label: labels[name] || name.replace(/_/g, " ").replace(/^./, (char) => char.toUpperCase()),
    target: typeof target === "string" ? target.replace(/\s+/g, " ").trim() : "",
  };
}
