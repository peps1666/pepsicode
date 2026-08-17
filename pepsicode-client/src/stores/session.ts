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

interface SessionState {
  messages: ChatMessage[];
  isRunning: boolean;
  sessionId: string | null;
  cwd: string;
  model: string;
  pendingPermission: PermissionRequest | null;
  initialize: () => Promise<void>;
  runTurn: (input: string) => Promise<void>;
  approvePermission: (decision: string, feedback?: string) => Promise<void>;
  listSessions: () => Promise<any[]>;
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

export const useSessionStore = create<SessionState>((set, get) => ({
  messages: [],
  isRunning: false,
  sessionId: null,
  cwd: "",
  model: "",
  pendingPermission: null,
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
            const messages = state.messages.map((m) =>
              m.isStreaming ? { ...m, isStreaming: false, content: data.content || m.content } : m
            );
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
          set({ isRunning: false });
          get().refreshCost();
          get().refreshContext();
          break;
        }
        case "session/saved": {
          // Session auto-saved by server
          break;
        }
      }
    });
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
      set({ isRunning: false });
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

  listSessions: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return [];
    const result = await sendRequest(ws, "session/list", {});
    return result.sessions || [];
  },

  resumeSession: async (sessionId: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    await sendRequest(ws, "session/resume", { session_id: sessionId });
    set({ sessionId, messages: [], isRunning: false });
    // Server-side manages full message state; client starts fresh.
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
    if (!ws) return;
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
    });
  },
}));
