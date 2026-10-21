import { create } from "zustand";
import { sendRequest, onMessage, useConnectionStore } from "./connection";
import type { SlashCommandInfo } from "../conversation/commandCompletion";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool_call" | "tool_result" | "progress";
  content: string;
  toolName?: string;
  toolInput?: Record<string, unknown>;
  isError?: boolean;
  isStreaming?: boolean;
  timestamp: number;
  entryId?: string;
  callId?: string;
  startedAt?: number;
  durationMs?: number;
  toolStatus?: string;
  exitCode?: number;
  activityKind?: "retry";
  retry?: { attempt: number; limit: number; delay_ms: number; reason: string; started_at?: number };
  verification?: VerificationReport;
}

export interface VerificationReport {
  status: string; reason: string; reason_code?: string; details?: string; ok: boolean;
}

export interface BranchNode {
  id: string;
  parent_id: string | null;
  role: "user" | "assistant";
  preview: string;
  depth: number;
  active: boolean;
}

export interface ConversationTree {
  nodes: BranchNode[];
  leaf_id: string | null;
  parent_session_id: string | null;
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

export type InputMode = "steer" | "follow_up";

export interface QueuedInput {
  id: string;
  content: string;
  mode: InputMode;
  display_content?: string | null;
}

interface SessionHistoryItem {
  entry_id?: string;
  role: "user" | "assistant" | "progress" | "tool_call" | "tool_result";
  content?: string;
  tool_name?: string;
  tool_input?: unknown;
  is_error?: boolean;
  call_id?: string;
  started_at?: number;
  duration_ms?: number;
  status?: string;
  exit_code?: number;
  activity_kind?: "retry";
  retry?: ChatMessage["retry"];
  verification?: VerificationReport;
}

interface SessionState {
  tree: ConversationTree | null;
  branchUpdating: boolean;
  branchError: string;
  refreshTree: () => Promise<void>;
  checkoutEntry: (id: string) => Promise<void>;
  forkSession: (id: string) => Promise<void>;
  cloneSession: () => Promise<void>;
  regenerate: (id: string) => Promise<void>;
  verification: VerificationReport | null;
  cacheHitPercentage: number | null;
  accessMode: "read_only" | "auto" | "full_access";
  permissionUpdating: boolean;
  updatePermissions: (mode: SessionState["accessMode"]) => Promise<void>;
  messages: ChatMessage[];
  isRunning: boolean;
  isStopping: boolean;
  cancelTurn: () => Promise<void>;
  pendingInputs: QueuedInput[];
  commands: SlashCommandInfo[];
  commandDiagnostics: string[];
  commandError: string;
  refreshCommands: (refresh?: boolean) => Promise<void>;
  draft: string;
  setDraft: (draft: string) => void;
  removeQueuedInput: (id: string) => Promise<void>;
  sessionId: string | null;
  cwd: string;
  model: string;
  provider: string;
  providers: Array<{ id: string; name: string; configured: boolean; baseUrl: string }>;
  loadProviderModels: (provider: string, refresh?: boolean) => Promise<{ models: ModelCapabilities[]; catalogError: string }>;
  probeProvider: (draft: ProviderDraft) => Promise<{ token: string; models: ModelCapabilities[]; catalogError: string; verified: boolean }>;
  saveProvider: (token: string, model: string) => Promise<void>;
  cancelProviderSetup: () => Promise<void>;
  reasoningEffort: string;
  models: ModelCapabilities[];
  catalogError: string;
  refreshModels: () => Promise<void>;
  isMock: boolean;
  modelUpdating: boolean;
  inspectModel: (model: string) => Promise<ModelCapabilities>;
  updateModel: (model: string, reasoningEffort: string, persist: boolean, provider?: string) => Promise<void>;
  pendingPermission: PermissionRequest | null;
  sessions: SessionMeta[];
  sessionsLoading: boolean;
  initialize: () => Promise<void>;
  createSession: () => Promise<void>;
  runTurn: (input: string, mode?: InputMode) => Promise<void>;
  approvePermission: (decision: string, feedback?: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  resumeSession: (sessionId: string, workspace?: string) => Promise<void>;
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

export interface ModelCapabilities {
  id: string;
  name: string;
  reasoningMode: string;
  reasoningLevels: string[];
}

export interface ProviderDraft {
  provider: string;
  name: string;
  baseUrl: string;
  authType: string;
  apiKey: string;
  modelsUrl: string | false;
}

function modelSettings(result: any) {
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
      entryId: item.entry_id,
      callId: item.call_id,
      startedAt: item.started_at,
      durationMs: item.duration_ms,
      toolStatus: item.status,
      exitCode: item.exit_code,
      activityKind: item.activity_kind,
      retry: item.retry,
      verification: item.verification,
    } satisfies ChatMessage];
  });
}

async function changeBranch(
  method: string, params: object,
  set: (value: Partial<SessionState>) => void, get: () => SessionState,
): Promise<void> {
  if (get().isRunning || get().modelUpdating || get().branchUpdating) throw new Error("请等待当前操作结束");
  set({ branchUpdating: true, branchError: "" });
  try {
    const { ws } = useConnectionStore.getState();
    await sendRequest(ws, method, params);
  } catch (error) {
    set({ branchError: String(error) });
    throw error;
  } finally {
    set({ branchUpdating: false });
  }
}

export const useSessionStore = create<SessionState>((set, get) => ({
  tree: null,
  branchUpdating: false,
  branchError: "",
  refreshTree: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws || !get().sessionId) return;
    const id = get().sessionId;
    try {
      const tree = await sendRequest(ws, "session/tree");
      if (get().sessionId === id) set({ tree: tree.nodes ? tree : null });
    } catch (error) {
      if (get().sessionId === id) set({ branchError: String(error) });
    }
  },
  checkoutEntry: async (id) => changeBranch("session/checkout", { entry_id: id }, set, get),
  forkSession: async (id) => changeBranch("session/fork", { entry_id: id }, set, get),
  cloneSession: async () => changeBranch("session/clone", {}, set, get),
  regenerate: async (id) => {
    if (get().isRunning || get().branchUpdating || get().modelUpdating) throw new Error("请等待当前操作结束");
    set({ isRunning: true, branchError: "", verification: null, cacheHitPercentage: null });
    const { ws } = useConnectionStore.getState();
    void sendRequest(ws, "turn/regenerate", { entry_id: id }, { timeoutMs: null }).catch((error) => {
      set((state) => ({
        isRunning: false, isStopping: false, branchError: String(error),
        draft: [...state.pendingInputs.map((item) => item.display_content || item.content), ...(state.draft ? [state.draft] : [])].join("\n\n"),
        pendingInputs: [],
      }));
    });
  },
  verification: null,
  cacheHitPercentage: null,
  accessMode: "auto",
  permissionUpdating: false,
  updatePermissions: async (mode) => {
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning || get().branchUpdating || get().permissionUpdating) return;
    set({ permissionUpdating: true });
    try {
      const result = await sendRequest(ws, "permissions/set", { mode });
      set({ accessMode: result.access_mode });
    } finally {
      set({ permissionUpdating: false });
    }
  },
  messages: [],
  isRunning: false,
  isStopping: false,
  cancelTurn: async () => {
    if (!get().isRunning || get().isStopping) return;
    set({ isStopping: true });
    try {
      const { ws } = useConnectionStore.getState();
      const result = await sendRequest(ws, "turn/cancel");
      if (!result.cancelled) set({ isStopping: false });
    } catch (error) {
      set({ isStopping: false });
      throw error;
    }
  },
  pendingInputs: [],
  commands: [],
  commandDiagnostics: [],
  commandError: "",
  refreshCommands: async (refresh = false) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    const sessionId = get().sessionId;
    const cwd = get().cwd;
    try {
      const result = await sendRequest(ws, "commands/list", { refresh });
      if (get().sessionId === sessionId && get().cwd === cwd) {
        set({ commands: result.commands || [], commandDiagnostics: result.diagnostics || [], commandError: "" });
      }
    } catch (error) {
      if (get().sessionId === sessionId && get().cwd === cwd) {
        set({ commandError: error instanceof Error ? error.message : String(error) });
      }
    }
  },
  draft: "",
  setDraft: (draft) => set({ draft }),
  removeQueuedInput: async (id) => {
    const { ws } = useConnectionStore.getState();
    const result = await sendRequest(ws, "turn/dequeue", { id });
    if (!result.removed) throw new Error("这条输入已经开始处理，无法取回");
  },
  sessionId: null,
  cwd: "",
  model: "",
  provider: "",
  providers: [],
  reasoningEffort: "default",
  models: [],
  catalogError: "",
  isMock: false,
  modelUpdating: false,
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
        accessMode: result.access_mode || "auto",
        ...modelSettings(result),
      });
      await get().refreshSessions();
      await Promise.all([get().refreshCost(), get().refreshContext(), get().refreshCommands()]);
    } catch (e) {
      console.error("Failed to initialize session:", e);
    }

    // Subscribe to events
    onMessage((message) => {
      if (message.kind !== "event") return;
      const { event, data } = message;

      switch (event) {
        case "session/view": {
          set((state) => ({
            sessionId: data.session_id,
            messages: hydrateSessionHistory(data.history),
            tree: data.tree || null,
            verification: data.verification || null,
            cacheHitPercentage: null,
            contextData: null,
            draft: [data.draft, state.draft].filter(Boolean).join("\n\n"),
          }));
          void get().refreshSessions();
          void get().refreshContext();
          break;
        }
        case "input/queue": {
          const delivered: QueuedInput[] = data.delivered || [];
          const returned: QueuedInput[] = data.returned || [];
          set((state) => ({
            pendingInputs: data.pending || [],
            draft: [...returned.map((item) => item.display_content || item.content), ...(state.draft ? [state.draft] : [])].join("\n\n"),
            messages: [...state.messages, ...delivered.map((item) => ({
              id: item.id, role: "user" as const, content: item.display_content || item.content, timestamp: Date.now(),
            }))],
          }));
          break;
        }
        case "verification/update": {
          set({ verification: data });
          break;
        }
        case "permissions/settings": {
          set({ accessMode: data.access_mode });
          break;
        }
        case "model/settings": {
          set({ ...modelSettings(data), cacheHitPercentage: null });
          void get().refreshContext();
          break;
        }
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
        case "request/retry": {
          set(state => ({ messages: [...state.messages, { id: nextMsgId(), role: "progress", content: data.content || "正在重试模型连接", timestamp: Date.now(), activityKind: "retry", retry: data.retry }] }));
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
                callId: data.call_id,
                startedAt: data.started_at,
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
                callId: data.call_id,
                startedAt: data.started_at,
                durationMs: data.duration_ms,
                toolStatus: data.status,
                exitCode: data.exit_code,
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
          set((state) => ({
            costData: { ...state.costData, ...data },
            cacheHitPercentage: data.cache_hit_percentage ?? null,
          }));
          break;
        }
        case "usage/update": {
          set((state) => ({
            cacheHitPercentage: data.cache_hit_percentage ?? null,
            contextData: data.context ? { ...state.contextData, ...data.context } : null,
          }));
          break;
        }
        case "turn/end": {
          set((state) => ({
            isRunning: false,
            isStopping: false,
            pendingPermission: null,
            messages: data.status === "error"
              ? appendError(state.messages, data.error)
              : state.messages,
          }));
          get().refreshCost();
          get().refreshContext();
          void get().refreshCommands();
          void get().refreshTree();
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
    if (!ws || get().isRunning || get().modelUpdating || get().branchUpdating) return;
    const result = await sendRequest(ws, "session/create", { cwd: get().cwd || undefined });
    set({ verification: null });
    set({
      sessionId: result.session_id,
      cwd: result.cwd,
      accessMode: result.access_mode || "auto",
      ...modelSettings(result),
      messages: [],
      tree: null,
      isRunning: false,
      pendingPermission: null,
      costData: null,
      cacheHitPercentage: null,
      contextData: null,
      planMode: false,
      planFile: null,
    });
    await get().refreshSessions();
    await Promise.all([get().refreshCost(), get().refreshContext(), get().refreshCommands()]);
  },

  runTurn: async (input: string, mode: InputMode = "steer") => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    if (get().modelUpdating || get().branchUpdating) throw new Error("请等待当前操作完成");
    if (get().isStopping) throw new Error("任务正在停止，请稍后发送");
    if (get().isRunning) {
      await sendRequest(ws, "turn/enqueue", { input, mode });
      return;
    }

    // Add user message
    const userMsg: ChatMessage = {
      id: nextMsgId(),
      role: "user",
      content: input,
      timestamp: Date.now(),
    };
    set((state) => ({ messages: [...state.messages, userMsg], isRunning: true, cacheHitPercentage: null, verification: null }));

    // Fire-and-forget: turn/run blocks until the agent turn completes.
    // Stream events (message/delta, tool/call, etc.) arrive in the meantime
    // via the onMessage subscription registered in initialize().
    sendRequest(ws, "turn/run", { input }, { timeoutMs: null }).catch((e) => {
      console.error("turn/run error:", e);
      set((state) => ({
        isRunning: false,
        isStopping: false,
        draft: [...state.pendingInputs.map((item) => item.display_content || item.content), ...(state.draft ? [state.draft] : [])].join("\n\n"),
        pendingInputs: [],
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

  resumeSession: async (sessionId: string, workspace?: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning || get().modelUpdating || get().permissionUpdating || get().branchUpdating) return;
    set({ isRunning: true, pendingPermission: null });
    try {
      const result = await sendRequest(ws, "session/resume", {
        session_id: sessionId,
        workspace: workspace || get().cwd,
      });
      set({
        sessionId: result.session_id,
        cwd: result.workspace,
        accessMode: result.access_mode || "auto",
        ...modelSettings(result),
        messages: hydrateSessionHistory(result.history),
        tree: result.tree || null,
        verification: result.verification || null,
        isRunning: false,
        pendingPermission: null,
        costData: null,
        cacheHitPercentage: null,
        contextData: null,
        planMode: result.permission_mode === "plan",
        planFile: result.plan_file || null,
        sessions: [],
      });
      await get().refreshSessions();
      void get().refreshCost();
      void get().refreshContext();
      void get().refreshCommands();
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
    const sessionId = get().sessionId;
    try {
      const result = await sendRequest(ws, "cost/query", {});
      if (get().sessionId === sessionId) {
        set({ costData: result, cacheHitPercentage: result.cache_hit_percentage ?? null });
      }
    } catch (e) {
      // ignore
    }
  },

  refreshContext: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    const sessionId = get().sessionId;
    try {
      const result = await sendRequest(ws, "context/query", {});
      if (get().sessionId === sessionId) set({ contextData: result });
    } catch (e) {
      // ignore
    }
  },

  switchCwd: async (cwd: string) => {
    set({ verification: null });
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning || get().modelUpdating || get().branchUpdating) return;
    // Re-create the session on the server with the new cwd. The server's
    // session/create reinitializes tools/permissions/model for the new workspace.
    const result = await sendRequest(ws, "session/create", { cwd });
    set({
      sessionId: result.session_id,
      cwd: result.cwd,
      accessMode: result.access_mode || "auto",
      ...modelSettings(result),
      messages: [],
      tree: null,
      isRunning: false,
      pendingPermission: null,
      costData: null,
      cacheHitPercentage: null,
      contextData: null,
      planMode: false,
      planFile: null,
      sessions: [],
    });
    await get().refreshSessions();
    await Promise.all([get().refreshCost(), get().refreshContext(), get().refreshCommands()]);
  },

  refreshModels: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    const result = await sendRequest(ws, "model/settings", { refresh: true });
    set(modelSettings(result));
  },

  loadProviderModels: async (provider: string, refresh = false) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    return await sendRequest(ws, "model/settings", { ...(provider ? { provider } : {}), refresh });
  },

  probeProvider: async (draft: ProviderDraft) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    return await sendRequest(ws, "provider/probe", { draft });
  },

  saveProvider: async (token: string, model: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    if (get().isRunning || get().modelUpdating || get().branchUpdating) throw new Error("请等待当前请求结束");
    set({ modelUpdating: true });
    try {
      const result = await sendRequest(ws, "provider/save", { token, model });
      set(modelSettings(result));
      void get().refreshContext();
    } finally { set({ modelUpdating: false }); }
  },

  cancelProviderSetup: async () => {
    const { ws } = useConnectionStore.getState();
    if (ws) await sendRequest(ws, "provider/cancel", {});
  },

  inspectModel: async (model: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    const result = await sendRequest(ws, "model/settings", { model });
    return result.capabilities;
  },

  updateModel: async (model: string, reasoningEffort: string, persist: boolean, provider?: string) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    if (get().isRunning || get().modelUpdating || get().branchUpdating) throw new Error("Wait for the current request to finish");
    set({ modelUpdating: true });
    try {
      const result = await sendRequest(ws, "model/update", { model, reasoningEffort, persist, ...(provider ? { provider } : {}) });
      set({ ...modelSettings(result), cacheHitPercentage: null });
      void get().refreshContext();
    } finally {
      set({ modelUpdating: false });
    }
  },
}));
