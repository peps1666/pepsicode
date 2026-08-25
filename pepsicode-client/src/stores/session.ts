import { create } from "zustand";
import { sendRequest, onMessage, useConnectionStore } from "./connection";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool_call" | "tool_result" | "progress";
  content: string;
  toolName?: string;
  toolInput?: Record<string, unknown>;
  isError?: boolean;
  isStreaming?: boolean;
  timestamp: number;
}

export interface PermissionRequest {
  prompt_id: string;
  kind: string;
  summary: string;
  details: string[];
  scope: string;
  choices: Array<{ key: string; label: string; decision: string }>;
}

export interface SessionMeta {
  session_id: string;
  created_at: number;
  updated_at: number;
  first_message: string;
  message_count: number;
  workspace: string;
}

interface SessionHistoryItem {
  role: "user" | "assistant" | "progress" | "tool_call" | "tool_result";
  content?: string;
  tool_name?: string;
  tool_input?: unknown;
  is_error?: boolean;
}

interface SessionState {
  messages: ChatMessage[];
  isRunning: boolean;
  sessionId: string | null;
  cwd: string;
  model: string;
  pendingPermission: PermissionRequest | null;
  sessions: SessionMeta[];
  sessionsLoading: boolean;
  initialize: () => Promise<void>;
  createSession: () => Promise<void>;
  runTurn: (input: string) => Promise<void>;
  approvePermission: (decision: string, feedback?: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  resumeSession: (sessionId: string) => Promise<void>;
  enterPlanMode: () => Promise<void>;
  exitPlanMode: () => Promise<void>;
  refreshCost: () => Promise<void>;
  refreshContext: () => Promise<void>;
  switchCwd: (cwd: string) => Promise<void>;
  costData: any;
  contextData: any;
  planMode: boolean;
  planFile: string | null;
}

let messageIdCounter = 0;
const nextMsgId = () => `msg-${++messageIdCounter}`;

function errorMessage(content: unknown): ChatMessage {
  const text = typeof content === "string" && content.trim()
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

function appendError(messages: ChatMessage[], content: unknown): ChatMessage[] {
  const next = errorMessage(content);
  const last = messages[messages.length - 1];
  return last?.isError && last.content === next.content ? messages : [...messages, next];
}

function hydrateSessionHistory(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  const baseTime = Date.now();
  const supportedRoles = new Set<SessionHistoryItem["role"]>([
    "user",
    "assistant",
    "progress",
    "tool_call",
    "tool_result",
  ]);

  return value.flatMap((raw, index) => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Partial<SessionHistoryItem>;
    if (!item.role || !supportedRoles.has(item.role)) return [];
    const toolInput = item.tool_input && typeof item.tool_input === "object" && !Array.isArray(item.tool_input)
      ? item.tool_input as Record<string, unknown>
      : undefined;
    return [{
      id: nextMsgId(),
      role: item.role,
      content: typeof item.content === "string" ? item.content : "",
      toolName: typeof item.tool_name === "string" ? item.tool_name : undefined,
      toolInput,
      isError: Boolean(item.is_error),
      timestamp: baseTime + index,
    } satisfies ChatMessage];
  });
}

export const useSessionStore = create<SessionState>((set, get) => ({
  messages: [],
  isRunning: false,
  sessionId: null,
  cwd: "",
  model: "",
  pendingPermission: null,
  sessions: [],
  sessionsLoading: false,
  costData: null,
  contextData: null,
  planMode: false,
  planFile: null,

  initialize: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    try {
      const result = await sendRequest(ws, "session/create", {});
      set({
        sessionId: result.session_id,
        cwd: result.cwd,
        model: result.model || "unknown",
      });
      await get().refreshSessions();
    } catch (e) {
      console.error("Failed to initialize session:", e);
    }

    // Subscribe to events
    onMessage((message) => {
      if (message.kind !== "event") return;
      const { event, data } = message;

      switch (event) {
        case "message/start": {
          const id = nextMsgId();
          set((state) => ({
            messages: [
              ...state.messages,
              { id, role: "assistant", content: "", isStreaming: true, timestamp: Date.now() },
            ],
          }));
          break;
        }
        case "message/delta": {
          set((state) => {
            const messages = [...state.messages];
            const last = messages[messages.length - 1];
            if (last && last.isStreaming) {
              messages[messages.length - 1] = { ...last, content: last.content + (data.text || "") };
            }
            return { messages };
          });
          break;
        }
        case "message/end": {
          set((state) => {
            const messages = [...state.messages];
            let streamingIndex = -1;
            for (let index = messages.length - 1; index >= 0; index -= 1) {
              if (messages[index].isStreaming) {
                streamingIndex = index;
                break;
              }
            }
            if (streamingIndex >= 0) {
              const streaming = messages[streamingIndex];
              messages[streamingIndex] = {
                ...streaming,
                role: data.role === "progress" ? "progress" : streaming.role,
                isStreaming: false,
                isError: Boolean(data.is_error),
                content: data.content || streaming.content,
              };
            } else if (data.content) {
              messages.push({
                id: nextMsgId(),
                role: "assistant",
                content: data.content,
                isError: Boolean(data.is_error),
                timestamp: Date.now(),
              });
            }
            return { messages };
          });
          break;
        }
        case "progress/message": {
          const id = nextMsgId();
          set((state) => ({
            messages: [
              ...state.messages,
              { id, role: "progress", content: data.content || "", timestamp: Date.now() },
            ],
          }));
          break;
        }
        case "tool/call": {
          const id = nextMsgId();
          set((state) => ({
            messages: [
              ...state.messages,
              {
                id,
                role: "tool_call",
                content: "",
                toolName: data.tool,
                toolInput: data.input,
                timestamp: Date.now(),
              },
            ],
          }));
          break;
        }
        case "tool/result": {
          const id = nextMsgId();
          set((state) => ({
            messages: [
              ...state.messages,
              {
                id,
                role: "tool_result",
                content: data.output || "",
                toolName: data.tool,
                isError: data.is_error,
                timestamp: Date.now(),
              },
            ],
          }));
          break;
        }
        case "permission/request": {
          set({ pendingPermission: data });
          break;
        }
        case "cost/update": {
          set((state) => ({ costData: { ...state.costData, ...data } }));
          // Also refresh full cost data periodically
          break;
        }
        case "turn/end": {
          set((state) => ({
            isRunning: false,
            messages: data.status === "error"
              ? appendError(state.messages, data.error)
              : state.messages,
          }));
          get().refreshCost();
          get().refreshContext();
          break;
        }
        case "session/saved": {
          void get().refreshSessions();
          break;
        }
      }
    });
  },

  createSession: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning) return;
    const result = await sendRequest(ws, "session/create", { cwd: get().cwd || undefined });
    set({
      sessionId: result.session_id,
      cwd: result.cwd,
      model: result.model || "unknown",
      messages: [],
      isRunning: false,
      pendingPermission: null,
      costData: null,
      contextData: null,
      planMode: false,
      planFile: null,
    });
    await get().refreshSessions();
  },

  runTurn: async (input: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning) return;

    // Add user message
    const userMsg: ChatMessage = {
      id: nextMsgId(),
      role: "user",
      content: input,
      timestamp: Date.now(),
    };
    set((state) => ({ messages: [...state.messages, userMsg], isRunning: true }));

    // Fire-and-forget: turn/run blocks until the agent turn completes.
    // Stream events (message/delta, tool/call, etc.) arrive in the meantime
    // via the onMessage subscription registered in initialize().
    sendRequest(ws, "turn/run", { input }).catch((e) => {
      console.error("turn/run error:", e);
      set((state) => ({
        isRunning: false,
        messages: appendError(state.messages, e instanceof Error ? e.message : e),
      }));
    });
  },

  approvePermission: async (decision: string, feedback?: string) => {
    const { ws } = useConnectionStore.getState();
    const { pendingPermission } = get();
    if (!ws || !pendingPermission) return;
    set({ pendingPermission: null });
    await sendRequest(ws, "tool/approve", {
      prompt_id: pendingPermission.prompt_id,
      decision,
      feedback: feedback || "",
    });
  },

  refreshSessions: async () => {
    const { ws } = useConnectionStore.getState();
    const { cwd } = get();
    if (!ws || !cwd) return;
    set({ sessionsLoading: true });
    try {
      const result = await sendRequest(ws, "session/list", { workspace: cwd });
      // Ignore a stale response that returns after the user switched folders.
      if (get().cwd === cwd) {
        set({ sessions: result.sessions || [] });
      }
    } catch (e) {
      if (get().cwd === cwd) {
        set({ sessions: [] });
      }
      console.error("Failed to load sessions:", e);
    } finally {
      if (get().cwd === cwd) {
        set({ sessionsLoading: false });
      }
    }
  },

  resumeSession: async (sessionId: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning) return;
    set({ isRunning: true, pendingPermission: null });
    try {
      const result = await sendRequest(ws, "session/resume", {
        session_id: sessionId,
        workspace: get().cwd,
      });
      set({
        sessionId: result.session_id,
        cwd: result.workspace,
        model: result.model || get().model,
        messages: hydrateSessionHistory(result.history),
        isRunning: false,
        pendingPermission: null,
        costData: null,
        contextData: null,
        planMode: result.permission_mode === "plan",
        planFile: result.plan_file || null,
      });
      void get().refreshCost();
      void get().refreshContext();
    } catch (e) {
      set((state) => ({
        isRunning: false,
        messages: appendError(state.messages, e instanceof Error ? e.message : e),
      }));
    }
  },

  enterPlanMode: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    const result = await sendRequest(ws, "plan/enter", {});
    set({ planMode: true, planFile: result.plan_file });
  },

  exitPlanMode: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    await sendRequest(ws, "plan/exit", {});
    set({ planMode: false, planFile: null });
  },

  refreshCost: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    try {
      const result = await sendRequest(ws, "cost/query", {});
      set({ costData: result });
    } catch (e) {
      // ignore
    }
  },

  refreshContext: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    try {
      const result = await sendRequest(ws, "context/query", {});
      set({ contextData: result });
    } catch (e) {
      // ignore
    }
  },

  switchCwd: async (cwd: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning) return;
    // Re-create the session on the server with the new cwd. The server's
    // session/create reinitializes tools/permissions/model for the new workspace.
    const result = await sendRequest(ws, "session/create", { cwd });
    set({
      sessionId: result.session_id,
      cwd: result.cwd,
      model: result.model || "unknown",
      messages: [],
      isRunning: false,
      pendingPermission: null,
      costData: null,
      contextData: null,
      planMode: false,
      planFile: null,
      sessions: [],
    });
    await get().refreshSessions();
  },
}));
