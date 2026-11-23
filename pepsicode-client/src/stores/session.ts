import { create } from "zustand";
import { sendRequest, onMessage, useConnectionStore } from "./connection";
import type {
  AgentSnapshot,
  AgentStatus,
  BranchNode,
  ChatMessage,
  ConversationTree,
  InputMode,
  ModelCapabilities,
  PermissionRequest,
  ProviderDraft,
  QueuedInput,
  SessionHistoryItem,
  SessionMeta,
  SessionState,
  TaskReport,
  VerificationReport,
} from "./sessionTypes";
import {
  appendError,
  applyMessageDelta,
  applyMessageEnd,
  hydrateSessionHistory,
  mergeQueuedInputs,
  modelSettings,
  nextMsgId,
  restoreDraftWithPending,
  settleStreaming,
  shouldApplyAgentSnapshot,
} from "./sessionTransforms";

export type {
  AgentSnapshot,
  AgentStatus,
  BranchNode,
  ChatMessage,
  ConversationTree,
  InputMode,
  ModelCapabilities,
  PermissionRequest,
  ProviderDraft,
  QueuedInput,
  SessionHistoryItem,
  SessionMeta,
  SessionState,
  TaskReport,
  VerificationReport,
};

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
  agents: null,
  stopWorker: async (traceId) => {
    const { ws } = useConnectionStore.getState();
    const id = get().sessionId;
    if (!ws || !id) throw new Error("PepsiCode 未连接");
    await sendRequest(ws, "agent/cancel", { session_id: id, trace_id: traceId });
    if (get().sessionId === id) await get().fetchAgents();
  },
  fetchAgents: async (offset = 0, traceId) => {
    const { ws } = useConnectionStore.getState();
    const id = get().sessionId;
    if (!ws || !id) return null;
    const snapshot: AgentSnapshot = await sendRequest(ws, "agent/snapshot", { session_id: id, offset, ...(traceId ? { trace_id: traceId } : {}) });
    if (get().sessionId !== id || snapshot.session_id !== id || snapshot.version !== 1) return null;
    if (offset === 0 && !traceId && snapshot.sequence >= (get().agents?.sequence ?? -1)) set({ agents: snapshot });
    return snapshot;
  },
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
    set({ isRunning: true, branchError: "", verification: null, task: null, cacheHitPercentage: null });
    const { ws } = useConnectionStore.getState();
    void sendRequest(ws, "turn/regenerate", { entry_id: id }, { timeoutMs: null }).catch((error) => {
      set((state) => ({
        isRunning: false, isStopping: false, branchError: String(error),
        messages: settleStreaming(state.messages, "interrupted"),
        draft: restoreDraftWithPending(state.pendingInputs, state.draft),
        pendingInputs: [],
      }));
    });
  },
  verification: null,
  task: null,
  cacheHitPercentage: null,
  latestUsage: null,
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
  setWorkspaceDefaultPermissions: async (mode) => {
    const { ws } = useConnectionStore.getState();
    if (!ws || get().isRunning || get().branchUpdating || get().permissionUpdating) throw new Error("当前无法保存工作区默认权限");
    set({ permissionUpdating: true });
    try {
      await sendRequest(ws, "permissions/set_workspace_default", { mode });
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
  serviceInfo: null,
  serviceNotifications: 0,

  initialize: async () => {
    const { ws } = useConnectionStore.getState();
    if (!ws) return;
    try {
      const result = await sendRequest(ws, "session/create", {});
      set({
        sessionId: result.session_id,
        agents: result.agents || null,
        cwd: result.cwd,
        accessMode: result.access_mode || "auto",
        serviceInfo: result.service
          ? { generation: String(result.generation), executionMode: String(result.execution_mode) }
          : null,
        ...modelSettings(result),
      });
      await get().refreshSessions();
      await Promise.all([get().refreshCost(), get().refreshContext(), get().refreshCommands()]);
    } catch (e) {
      console.error("Failed to initialize session:", e);
    }
    get().serviceInfo && void get().refreshServiceNotifications();

    let agentResync: { sessionId: string } | undefined;
    // Subscribe to events
    onMessage((message) => {
      if (message.kind !== "event") return;
      const { event, data } = message;

      switch (event) {
        case "connection/ready": {
          if (data.service) {
            set({ serviceInfo: { generation: String(data.generation ?? ""), executionMode: String(data.execution_mode ?? data.executionMode ?? "service") } });
            void get().refreshServiceNotifications();
          }
          break;
        }
        case "agent/update": {
          const snapshot: AgentSnapshot = data;
          const previous = get().agents;
          if (!shouldApplyAgentSnapshot(previous, snapshot, get().sessionId)) break;
          // Updates carry a complete first page. Apply immediately and confirm
          // a gap via the active-session snapshot endpoint.
          set({ agents: snapshot });
          if (previous && snapshot.sequence > previous.sequence + 1 && agentResync?.sessionId !== snapshot.session_id) {
            const pending = { sessionId: snapshot.session_id };
            agentResync = pending;
            void get().fetchAgents().catch(() => {}).finally(() => {
              if (agentResync === pending) agentResync = undefined;
            });
          }
          break;
        }
        case "session/view": {
          set((state) => ({
            sessionId: data.session_id,
            agents: data.session_id === state.sessionId && state.agents
              && state.agents.sequence > (data.agents?.sequence ?? -1) ? state.agents : data.agents || null,
            messages: hydrateSessionHistory(data.history),
            tree: data.tree || null,
            verification: data.verification || null,
            task: data.task || null,
            pendingInputs: data.pending_inputs || [],
            cacheHitPercentage: null,
            contextData: null,
            draft: [data.draft, state.draft].filter(Boolean).join("\n\n"),
          }));
          void get().refreshSessions();
          void get().refreshContext();
          break;
        }
        case "input/queue": {
          set((state) => mergeQueuedInputs(state, data));
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
          set((state) => ({ messages: applyMessageDelta(state.messages, data.text || "") }));
          break;
        }
        case "message/end": {
          set((state) => ({ messages: applyMessageEnd(state.messages, data) }));
          break;
        }
        case "thinking/start": {
          if (typeof data.id !== "string") break;
          set(state => ({ messages: [...state.messages, {
            id: nextMsgId(), role: "thinking", content: "", thinkingId: data.id,
            isStreaming: true, timestamp: Date.now(),
          }] }));
          break;
        }
        case "thinking/delta": {
          set(state => ({ messages: state.messages.map(message =>
            message.role === "thinking" && message.thinkingId === data.id && message.isStreaming
              ? { ...message, content: message.content + (typeof data.text === "string" ? data.text : "") }
              : message) }));
          break;
        }
        case "thinking/end": {
          set(state => ({ messages: state.messages.map(message =>
            message.role === "thinking" && message.thinkingId === data.id && message.isStreaming
              ? { ...message, isStreaming: false, thinkingStatus: data.status === "cancelled" ? "cancelled" : data.status === "interrupted" ? "interrupted" : "completed" }
              : message) }));
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
        case "permission/withdrawn": {
          if (get().pendingPermission?.prompt_id === data.prompt_id) set({ pendingPermission: null });
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
            latestUsage: data.input_total == null ? null : { input_total: data.input_total, cache_read: data.cache_read ?? null, cache_write: data.cache_write ?? null, cost_usd: data.cost_usd ?? null },
            contextData: data.context ? { ...state.contextData, ...data.context } : null,
          }));
          break;
        }
        case "turn/end": {
          set((state) => ({
            isRunning: false,
            task: data.task ?? state.task,
            isStopping: false,
            pendingPermission: null,
            messages: settleStreaming(data.status === "error"
              ? appendError(state.messages, data.error)
              : state.messages, data.status === "cancelled" ? "cancelled" : "interrupted"),
          }));
          get().refreshCost();
          get().refreshContext();
          void get().refreshCommands();
          void get().refreshTree();
          if (get().serviceInfo) void get().refreshServiceNotifications();
          break;
        }
        case "session/saved": {
          void get().refreshSessions();
          break;
        }
      }
    });
  },

  createSession: async (workspace) => {
    const { ws } = useConnectionStore.getState();
    const { cwd, isRunning, modelUpdating, branchUpdating, permissionUpdating } = get();
    if (!ws || isRunning || modelUpdating || branchUpdating || permissionUpdating) return;
    const result = await sendRequest(ws, "session/create", { cwd: workspace ?? (cwd || undefined) });
    set({
      sessionId: result.session_id,
      agents: result.agents || null,
      cwd: result.cwd,
      ...(result.cwd !== cwd ? { sessions: [] } : {}),
      accessMode: result.access_mode || "auto",
      ...modelSettings(result),
      verification: null,
      task: null,
      pendingInputs: [],
      latestUsage: null,
      branchError: "",
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

  runTurn: async (input: string, mode: InputMode = "steer", answerTo: string[] = []) => {
    const { ws } = useConnectionStore.getState();
    if (!ws) throw new Error("Server disconnected");
    if (get().modelUpdating || get().branchUpdating) throw new Error("请等待当前操作完成");
    if (get().isStopping) throw new Error("任务正在停止，请稍后发送");
    if (get().isRunning) {
      await sendRequest(ws, "turn/enqueue", { input, mode, ...(answerTo.length ? { answer_to: answerTo } : {}) });
      return;
    }

    // Add user message
    const userMsg: ChatMessage = {
      id: nextMsgId(),
      role: "user",
      content: input,
      timestamp: Date.now(),
    };
    set((state) => ({ messages: [...state.messages, userMsg], isRunning: true, cacheHitPercentage: null, verification: null, task: null }));

    // Fire-and-forget: turn/run blocks until the agent turn completes.
    // Stream events (message/delta, tool/call, etc.) arrive in the meantime
    // via the onMessage subscription registered in initialize().
    sendRequest(ws, "turn/run", { input, ...(answerTo.length ? { answer_to: answerTo } : {}) }, { timeoutMs: null }).catch((e) => {
      console.error("turn/run error:", e);
      set((state) => ({
        isRunning: false,
        isStopping: false,
        draft: restoreDraftWithPending(state.pendingInputs, state.draft),
        pendingInputs: [],
        messages: settleStreaming(appendError(state.messages, e instanceof Error ? e.message : e), "interrupted"),
      }));
    });
  },

  approvePermission: async (decision: string, feedback?: string) => {
    const { ws } = useConnectionStore.getState();
    const { pendingPermission } = get();
    if (!pendingPermission) return;
    if (!ws) throw new Error("本地服务已断开连接");
    const result = await sendRequest(ws, "tool/approve", {
      prompt_id: pendingPermission.prompt_id,
      decision,
      feedback: feedback || "",
    });
    if (result.resolved === false) throw new Error("这项权限请求已结束");
    if (get().pendingPermission?.prompt_id === pendingPermission.prompt_id) set({ pendingPermission: null });
  },

  refreshServiceNotifications: async () => {
    // Service outbox catch-up (M3): count merged pending notifications so the
    // banner can show them without spamming the chat timeline.
    if (!get().serviceInfo) return;
    const { ws } = useConnectionStore.getState();
    try {
      const result = await sendRequest(ws, "notifications/fetch", { cursor: 0 });
      const items = Array.isArray(result.notifications) ? result.notifications : [];
      set({ serviceNotifications: items.filter((item: any) => item.status !== "uncertain").length });
    } catch {
      // Older kernels without the API simply show no count.
    }
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
        agents: result.agents || null,
        cwd: result.workspace,
        accessMode: result.access_mode || "auto",
        ...modelSettings(result),
        messages: hydrateSessionHistory(result.history),
        tree: result.tree || null,
        verification: result.verification || null,
        task: result.task || null,
        pendingInputs: result.pending_inputs || [],
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

  switchCwd: (cwd) => get().createSession(cwd),

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
