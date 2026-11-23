import type {
  AgentSnapshot,
  ChatMessage,
  QueuedInput,
  SessionHistoryItem,
  SessionState,
} from "./sessionTypes";

export function modelSettings(result: any) {
  return {
    model: result.model || "unknown",
    provider: result.provider || "",
    providers: result.providers || [],
    reasoningEffort: result.reasoningEffort || "default",
    models: result.models || [],
    catalogError: result.catalogError || "",
    isMock: Boolean(result.isMock),
  };
}

let messageIdCounter = 0;
export const nextMsgId = () => `msg-${++messageIdCounter}`;

export function errorMessage(content: unknown): ChatMessage {
  const text =
    typeof content === "string" && content.trim()
      ? content.trim()
      : "The request failed. Check the server log for details.";
  return {
    id: nextMsgId(),
    role: "assistant",
    content: text,
    isError: true,
    timestamp: Date.now(),
  };
}

export function appendError(messages: ChatMessage[], content: unknown): ChatMessage[] {
  const next = errorMessage(content);
  const last = messages[messages.length - 1];
  return last?.isError && last.content === next.content ? messages : [...messages, next];
}

export function settleStreaming(messages: ChatMessage[], status: "cancelled" | "interrupted"): ChatMessage[] {
  return messages.map((message) =>
    message.isStreaming
      ? {
          ...message,
          isStreaming: false,
          ...(message.role === "thinking" ? { thinkingStatus: status } : {}),
        }
      : message,
  );
}

export function hydrateSessionHistory(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  const baseTime = Date.now();
  const supportedRoles = new Set<SessionHistoryItem["role"]>([
    "user",
    "assistant",
    "progress",
    "thinking",
    "tool_call",
    "tool_result",
  ]);

  return value.flatMap((raw, index) => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Partial<SessionHistoryItem>;
    if (!item.role || !supportedRoles.has(item.role)) return [];
    const toolInput =
      item.tool_input && typeof item.tool_input === "object" && !Array.isArray(item.tool_input)
        ? (item.tool_input as Record<string, unknown>)
        : undefined;
    return [
      {
        id: nextMsgId(),
        role: item.role,
        content: typeof item.content === "string" ? item.content : "",
        toolName: typeof item.tool_name === "string" ? item.tool_name : undefined,
        toolInput,
        isError: Boolean(item.is_error),
        timestamp: baseTime + index,
        entryId: item.entry_id,
        callId: item.call_id,
        startedAt: item.started_at,
        durationMs: item.duration_ms,
        toolStatus: item.status,
        exitCode: item.exit_code,
        activityKind: item.activity_kind,
        retry: item.retry,
        verification: item.verification,
        task: item.task,
      } satisfies ChatMessage,
    ];
  });
}

export function restoreDraftWithPending(pendingInputs: QueuedInput[], draft: string): string {
  return [...pendingInputs.map((item) => item.display_content || item.content), ...(draft ? [draft] : [])].join(
    "\n\n",
  );
}

export function applyMessageDelta(messages: ChatMessage[], text: string): ChatMessage[] {
  const next = [...messages];
  const last = next[next.length - 1];
  if (last?.role === "assistant" && last.isStreaming) {
    next[next.length - 1] = { ...last, content: last.content + text };
  }
  return next;
}

export function applyMessageEnd(
  messages: ChatMessage[],
  data: { role?: string; is_error?: boolean; content?: string },
): ChatMessage[] {
  const next = [...messages];
  let streamingIndex = -1;
  for (let index = next.length - 1; index >= 0; index -= 1) {
    if (next[index].role === "assistant" && next[index].isStreaming) {
      streamingIndex = index;
      break;
    }
  }
  if (streamingIndex >= 0) {
    const streaming = next[streamingIndex];
    next[streamingIndex] = {
      ...streaming,
      role: data.role === "progress" ? "progress" : streaming.role,
      isStreaming: false,
      isError: Boolean(data.is_error),
      content: data.content || streaming.content,
    };
  } else if (data.content) {
    next.push({
      id: nextMsgId(),
      role: "assistant",
      content: data.content,
      isError: Boolean(data.is_error),
      timestamp: Date.now(),
    });
  }
  return next;
}

export function mergeQueuedInputs(
  state: Pick<SessionState, "messages" | "draft">,
  data: { delivered?: QueuedInput[]; returned?: QueuedInput[]; pending?: QueuedInput[] },
): Pick<SessionState, "pendingInputs" | "draft" | "messages"> {
  const delivered: QueuedInput[] = data.delivered || [];
  const returned: QueuedInput[] = data.returned || [];
  return {
    pendingInputs: data.pending || [],
    draft: restoreDraftWithPending(returned, state.draft),
    messages: [
      ...state.messages,
      ...delivered.map((item) => ({
        id: item.id,
        role: "user" as const,
        content: item.display_content || item.content,
        timestamp: Date.now(),
      })),
    ],
  };
}

export function shouldApplyAgentSnapshot(
  previous: AgentSnapshot | null,
  snapshot: AgentSnapshot,
  activeSessionId: string | null,
): boolean {
  return !(
    snapshot.version !== 1 ||
    snapshot.session_id !== activeSessionId ||
    (previous && snapshot.sequence <= previous.sequence)
  );
}
