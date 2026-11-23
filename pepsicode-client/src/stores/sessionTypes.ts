import type { SlashCommandInfo } from "../conversation/commandCompletion";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system" | "tool_call" | "tool_result" | "progress" | "thinking";
  content: string;
  toolName?: string;
  toolInput?: Record<string, unknown>;
  isError?: boolean;
  isStreaming?: boolean;
  thinkingId?: string;
  thinkingStatus?: "completed" | "cancelled" | "interrupted";
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
  task?: TaskReport;
}

export interface TaskReport {
  id: string;
  revision: number;
  objective: string;
  status: string;
  criteria: Array<{ id: string; kind: string; path?: string; argv?: string[]; text?: string; purpose?: string }>;
  instructions?: string[];
  evidence: Array<{
    criterion_id: string;
    status: string;
    task_revision?: number;
    acceptance_run_id?: string;
    workspace_digest?: string;
  }>;
  assurance?: string;
  acceptance?: {
    valid: boolean;
    task_revision: number;
    kind?: "worker_reports";
    acceptance_run_id?: string;
    workspace?: { digest: string };
    reason_code?: string;
  } | null;
}

export interface VerificationReport {
  status: string;
  reason: string;
  reason_code?: string;
  details?: string;
  ok: boolean;
}

export interface AgentStatus {
  trace_id: string;
  parent_id: string | null;
  parent_call_id: string | null;
  name: string;
  task: string;
  stage: string;
  status: string;
  elapsed_seconds: number;
  started_at: number;
  ended_at: number | null;
  historical: boolean;
  stale: boolean;
  end_reason: string | null;
  result_summary: string;
  can_stop?: boolean;
  can_reply_running?: boolean;
  activities: Array<{ id: string; kind: string; summary: string }>;
  report: { id: string; status: string; summary: string; submitted_at: number } | null;
  questions: Array<{
    id: string;
    question: string;
    required: boolean;
    status: "pending" | "answered";
    actionable: boolean;
  }>;
  acceptance: { passed: number; total: number };
  recent_tools: Array<{
    id: string;
    tool: string;
    summary: string;
    status: string;
    input_preview: string;
    output: string;
    duration_ms: number;
  }>;
}

export interface AgentSnapshot {
  version: number;
  session_id: string;
  sequence: number;
  records: AgentStatus[];
  next_offset: number | null;
  persistence_error: string | null;
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

export interface SessionHistoryItem {
  task?: TaskReport;
  entry_id?: string;
  role: "user" | "assistant" | "progress" | "tool_call" | "tool_result" | "thinking";
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

export interface SessionState {
  agents: AgentSnapshot | null;
  fetchAgents: (offset?: number, traceId?: string) => Promise<AgentSnapshot | null>;
  stopWorker: (traceId: string) => Promise<void>;
  tree: ConversationTree | null;
  branchUpdating: boolean;
  branchError: string;
  refreshTree: () => Promise<void>;
  checkoutEntry: (id: string) => Promise<void>;
  forkSession: (id: string) => Promise<void>;
  cloneSession: () => Promise<void>;
  regenerate: (id: string) => Promise<void>;
  verification: VerificationReport | null;
  task: TaskReport | null;
  cacheHitPercentage: number | null;
  latestUsage: {
    input_total: number | null;
    cache_read: number | null;
    cache_write: number | null;
    cost_usd: number | null;
  } | null;
  accessMode: "ask" | "auto" | "full_access" | "read_only";
  permissionUpdating: boolean;
  updatePermissions: (mode: "ask" | "auto" | "full_access") => Promise<void>;
  setWorkspaceDefaultPermissions: (mode: "ask" | "auto" | "full_access") => Promise<void>;
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
  loadProviderModels: (
    provider: string,
    refresh?: boolean,
  ) => Promise<{ models: ModelCapabilities[]; catalogError: string }>;
  probeProvider: (
    draft: ProviderDraft,
  ) => Promise<{ token: string; models: ModelCapabilities[]; catalogError: string; verified: boolean }>;
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
  createSession: (workspace?: string) => Promise<void>;
  runTurn: (input: string, mode?: InputMode, answerTo?: string[]) => Promise<void>;
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
  // M3 resident service: announced via connection/ready. Detaching only ends
  // this control view — authorized service work keeps running.
  serviceInfo: { generation: string; executionMode: string } | null;
  serviceNotifications: number;
  refreshServiceNotifications: () => Promise<void>;
}
