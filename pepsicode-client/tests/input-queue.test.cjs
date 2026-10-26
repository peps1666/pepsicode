const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const ts = require("typescript");

async function session() {
  const handlers = [];
  const calls = [];
  const policies = [];
  let enqueueError;
  let failTurn;
  let branchError;
  let approval;
  const connection = {
    useConnectionStore: { getState: () => ({ ws: {} }) },
    onMessage: (handler) => handlers.push(handler),
    sendRequest: async (_ws, method, params, options) => {
      calls.push({ method, params });
      policies.push({ method, options });
      if (method === "session/create") return { session_id: "test", cwd: "/project" };
      if (method === "commands/list") return { commands: [
        { name: "/review", usage: "/review [focus]", description: "Review changes", source: "project" },
      ], diagnostics: ["invalid command file"] };
      if (["turn/run", "turn/regenerate"].includes(method)) return new Promise((_resolve, reject) => { failTurn = reject; });
      if (method.startsWith("session/") && branchError) throw branchError;
      if (method === "session/resume") return {
        session_id: params.session_id, workspace: params.workspace, history: [],
      };
      if (method === "session/list" && params.workspace === "/another-project") return {
        sessions: [{ session_id: "other-chat", workspace: params.workspace }],
      };
      if (method === "turn/enqueue" && enqueueError) throw enqueueError;
      if (method === "turn/cancel") return { cancelled: true };
      if (method === "turn/dequeue") return { removed: true };
      if (method === "tool/approve" && approval) return approval();
      return {};
    },
  };
  const source = readFileSync(join(__dirname, "../src/stores/session.ts"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const result = {};
  new Function("require", "exports", compiled)(
    (id) => id === "./connection" ? connection : require(id), result,
  );
  const store = result.useSessionStore;
  await store.getState().initialize();
  return {
    store, calls, policies,
    emit: (data) => handlers.forEach((handler) => handler({ kind: "event", event: "input/queue", data })),
    event: (event, data) => handlers.forEach((handler) => handler({ kind: "event", event, data })),
    rejectEnqueue: () => { enqueueError = new Error("task is finishing"); },
    disconnect: () => failTurn(new Error("WebSocket connection closed")),
    rejectBranch: () => { branchError = new Error("invalid branch"); },
    onApproval: (handler) => { approval = handler; },
  };
}

test("thinking streams independently, closes on cancellation, and survives history replacement", async () => {
  const api = await session();
  api.event("thinking/start", {id:"thought-1"});
  api.event("thinking/delta", {id:"thought-1",text:"先读取"});
  api.event("thinking/delta", {id:"stale",text:"must not appear"});
  api.event("thinking/delta", {id:"thought-1",text:"项目。"});
  api.event("thinking/end", {id:"thought-1",status:"completed"});
  api.event("message/start", {});
  api.event("message/delta", {text:"Answer"});
  api.event("message/end", {content:"Answer"});
  assert.deepEqual(api.store.getState().messages.map(m=>[m.role,m.content,m.isStreaming]), [["thinking","先读取项目。",false],["assistant","Answer",false]]);
  api.event("thinking/start", {id:"thought-2"});
  api.event("thinking/delta", {id:"thought-2",text:"Partial"});
  api.event("turn/end", {status:"cancelled"});
  assert.equal(api.store.getState().messages.at(-1).isStreaming,false);
  assert.equal(api.store.getState().messages.at(-1).thinkingStatus,"cancelled");
  api.event("session/view", {session_id:"test",history:[{role:"thinking",content:"先读取项目。"},{role:"assistant",content:"Answer"}]});
  assert.deepEqual(api.store.getState().messages.map(m=>[m.role,m.content]), [["thinking","先读取项目。"],["assistant","Answer"]]);
});

test("failed approval retains the request; late replies cannot erase a newer request", async () => {
  const api=await session(),request={prompt_id:"first",kind:"command",summary:"run",details:[],scope:"python",choices:[]};
  api.event("permission/request",request);
  api.onApproval(async()=>{throw new Error("offline");});
  await assert.rejects(api.store.getState().approvePermission("allow_once"),/offline/);
  assert.equal(api.store.getState().pendingPermission.prompt_id,"first");
  let finish;api.onApproval(()=>new Promise(resolve=>{finish=resolve;}));
  const pending=api.store.getState().approvePermission("allow_once");
  api.event("permission/request",{...request,prompt_id:"second"});
  finish({resolved:true});await pending;
  assert.equal(api.store.getState().pendingPermission.prompt_id,"second");
  api.onApproval(async()=>({resolved:true}));
  await api.store.getState().approvePermission("deny_once");
  assert.equal(api.store.getState().pendingPermission,null);
});

test("execution IDs, timing, retries and verification survive authoritative history replacement", async () => {
  const api = await session();
  api.event("tool/call", {tool:"read_file",input:{path:"a"},call_id:"actual",started_at:1000});
  api.event("tool/result", {tool:"read_file",output:"Failed",is_error:true,call_id:"actual",started_at:1000,duration_ms:42,status:"failed",exit_code:1});
  api.event("request/retry", {content:"Retry",retry:{attempt:1,limit:2,delay_ms:500,reason:"HTTP 503"}});
  assert.equal(api.store.getState().messages[1].durationMs,42);
  assert.equal(api.store.getState().messages[2].activityKind,"retry");
  const verification={status:"not_run",reason_code:"no_checks",reason:"No checks",ok:false};
  const task={id:"task",revision:2,objective:"Implement feature",status:"incomplete",criteria:[],evidence:[]};
  api.event("session/view", {session_id:"test",history:[
    {role:"tool_call",tool_name:"read_file",call_id:"actual",tool_input:{path:"a"}},
    {role:"tool_result",tool_name:"read_file",call_id:"actual",started_at:1000,duration_ms:42,status:"failed",exit_code:1,content:"Failed",is_error:true},
    {role:"progress",content:"Retry",activity_kind:"retry",retry:{attempt:1,limit:2,delay_ms:500,reason:"HTTP 503"}},
    {role:"assistant",content:"Answer",verification,task},
  ],task,pending_inputs:[{id:"restored",content:"next",mode:"follow_up"}]});
  const messages=api.store.getState().messages;
  assert.equal(messages[0].callId,messages[1].callId);
  assert.equal(messages[1].durationMs,42);
  assert.equal(messages[2].retry.attempt,1);
  assert.deepEqual(messages[3].verification,verification);
  assert.deepEqual(messages[3].task,task);
  assert.deepEqual(api.store.getState().task,task);
  assert.equal(api.store.getState().pendingInputs[0].id,"restored");
});

test("task completion cannot survive a new failed turn or a branch without its authority", async () => {
  const api = await session();
  const task={id:"first",revision:2,objective:"Implement feature",status:"complete",criteria:[],evidence:[]};
  api.event("session/view", {session_id:"test",history:[],task});
  await api.store.getState().runTurn("Implement another feature");
  assert.equal(api.store.getState().task,null);
  const failed={...task,id:"second",status:"budget_exhausted"};
  api.event("turn/end", {status:"error",error:"Budget exhausted",task:failed});
  assert.deepEqual(api.store.getState().task,failed);
  assert.equal(api.store.getState().isRunning,false);
  api.event("session/view", {session_id:"test",history:[]});
  assert.equal(api.store.getState().task,null);
});

test("resuming a conversation in another project sends its workspace and refreshes that project's list", async () => {
  const api = await session();
  await api.store.getState().resumeSession("other-chat", "/another-project");
  assert.deepEqual(api.calls.find((call) => call.method === "session/resume"), {
    method: "session/resume", params: { session_id: "other-chat", workspace: "/another-project" },
  });
  assert.equal(api.store.getState().cwd, "/another-project");
  assert.equal(api.store.getState().sessionId, "other-chat");
  assert.equal(api.store.getState().sessions[0].workspace, "/another-project");
});

test("a running turn blocks project switching", async () => {
  const api = await session();
  await api.store.getState().runTurn("working");
  await api.store.getState().resumeSession("other-chat", "/another-project");
  assert.equal(api.calls.some((call) => call.method === "session/resume"), false);
  assert.equal(api.store.getState().cwd, "/project");
});

test("busy sends use the input queue and enter the transcript only when delivered", async () => {
  const api = await session();
  await api.store.getState().runTurn("initial");
  assert.deepEqual(api.policies.at(-1), { method: "turn/run", options: { timeoutMs: null } });
  await api.store.getState().runTurn("next", "follow_up");
  assert.deepEqual(api.calls.at(-1), { method: "turn/enqueue", params: { input: "next", mode: "follow_up" } });
  assert.deepEqual(api.store.getState().messages.map((item) => item.content), ["initial"]);
  const item = { id: "queued", content: "next", mode: "follow_up" };
  api.emit({ pending: [item] });
  assert.equal(api.store.getState().pendingInputs.length, 1);
  api.emit({ pending: [], delivered: [item] });
  assert.deepEqual(api.store.getState().messages.map((item) => item.content), ["initial", "next"]);
});

test("cancelled or retrieved inputs return to the draft without erasing current edits", async () => {
  const api = await session();
  api.store.getState().setDraft("still editing");
  api.emit({ pending: [], returned: [
    { id: "1", content: "first", mode: "steer" },
    { id: "2", content: "second", mode: "follow_up" },
  ] });
  assert.equal(api.store.getState().draft, "first\n\nsecond\n\nstill editing");
  assert.equal(api.store.getState().messages.length, 0);
});

test("queue rejection preserves running state and does not invent a user message", async () => {
  const api = await session();
  await api.store.getState().runTurn("initial");
  api.rejectEnqueue();
  await assert.rejects(api.store.getState().runTurn("late"), /finishing/);
  assert.equal(api.store.getState().isRunning, true);
  assert.deepEqual(api.store.getState().messages.map((item) => item.content), ["initial"]);
});

test("stop waits for turn settlement and blocks further queue submissions", async () => {
  const api = await session();
  await api.store.getState().runTurn("initial");
  await api.store.getState().cancelTurn();
  assert.equal(api.store.getState().isRunning, true);
  assert.equal(api.store.getState().isStopping, true);
  await assert.rejects(api.store.getState().runTurn("too late"), /停止/);
  api.event("turn/end", { status: "cancelled" });
  assert.equal(api.store.getState().isRunning, false);
  assert.equal(api.store.getState().isStopping, false);
});

test("a dropped connection retains queued input and the current draft", async () => {
  const api = await session();
  await api.store.getState().runTurn("initial");
  api.event("thinking/start", {id:"disconnect-thought"});
  api.event("thinking/delta", {id:"disconnect-thought",text:"Partial"});
  api.emit({ pending: [{ id: "pending", content: "queued work", mode: "follow_up" }] });
  api.store.getState().setDraft("still editing");
  api.disconnect();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(api.store.getState().draft, "queued work\n\nstill editing");
  assert.deepEqual(api.store.getState().pendingInputs, []);
  assert.equal(api.store.getState().isRunning, false);
  assert.equal(api.store.getState().messages.find(m=>m.role==="thinking").thinkingStatus,"interrupted");
});

test("command discovery is available before typing and supports explicit refresh", async () => {
  const api = await session();
  assert.equal(api.store.getState().commands[0].name, "/review");
  assert.deepEqual(api.store.getState().commandDiagnostics, ["invalid command file"]);
  await api.store.getState().refreshCommands(true);
  assert.deepEqual(api.calls.at(-1), { method: "commands/list", params: { refresh: true } });
});

test("queued Markdown commands display and restore the original invocation", async () => {
  const api = await session();
  const command = { id: "cmd", content: "Review correctness and security.", mode: "follow_up", display_content: "/review security" };
  api.emit({ pending: [command] });
  api.emit({ pending: [], delivered: [command] });
  assert.equal(api.store.getState().messages.at(-1).content, "/review security");
  api.emit({ pending: [], returned: [command] });
  assert.equal(api.store.getState().draft, "/review security");
});

test("branch views replace the timeline and preserve the current draft", async () => {
  const api = await session();
  api.store.getState().setDraft("current edit");
  await api.store.getState().forkSession("user-entry");
  assert.deepEqual(api.calls.at(-1), { method: "session/fork", params: { entry_id: "user-entry" } });
  api.event("session/view", { session_id: "child", draft: "original question", history: [
    { role: "user", content: "/review", entry_id: "entry-one" },
    { role: "assistant", content: "earlier answer", entry_id: "entry-two" },
  ], tree: { nodes: [], leaf_id: "entry-two", parent_session_id: "test" } });
  assert.equal(api.store.getState().sessionId, "child");
  assert.deepEqual(api.store.getState().messages.map(item => item.entryId), ["entry-one", "entry-two"]);
  assert.equal(api.store.getState().draft, "original question\n\ncurrent edit");
  assert.equal(api.store.getState().tree.parent_session_id, "test");
  assert.equal(api.store.getState().branchUpdating, false);
});

test("invalid or busy branch requests leave the active timeline intact", async () => {
  const api = await session();
  api.rejectBranch();
  await assert.rejects(api.store.getState().checkoutEntry("missing"), /invalid branch/);
  assert.equal(api.store.getState().branchUpdating, false);
  assert.match(api.store.getState().branchError, /invalid branch/);
  await api.store.getState().runTurn("active task");
  const before = api.calls.length;
  await assert.rejects(api.store.getState().cloneSession(), /等待/);
  await assert.rejects(api.store.getState().regenerate("one"), /等待/);
  assert.equal(api.calls.length, before);
  assert.deepEqual(api.store.getState().messages.map(item => item.content), ["active task"]);
});

test("regeneration waits for authoritative history and restores queued drafts on disconnect", async () => {
  const api = await session();
  api.event("session/view", { session_id: "test", history: [{ role: "user", content: "original", entry_id: "one" }] });
  await api.store.getState().regenerate("one");
  assert.deepEqual(api.policies.at(-1), { method: "turn/regenerate", options: { timeoutMs: null } });
  assert.deepEqual(api.calls.at(-1), { method: "turn/regenerate", params: { entry_id: "one" } });
  assert.equal(api.store.getState().isRunning, true);
  assert.equal(api.store.getState().messages.length, 1);
  api.event("thinking/start", {id:"regeneration-thought"});
  api.event("thinking/delta", {id:"regeneration-thought",text:"Regenerated thought"});
  api.emit({ pending: [{ id: "pending", content: "next task", mode: "follow_up" }] });
  api.store.getState().setDraft("editing");
  api.disconnect();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(api.store.getState().draft, "next task\n\nediting");
  assert.equal(api.store.getState().isRunning, false);
  assert.deepEqual(api.store.getState().pendingInputs, []);
  assert.match(api.store.getState().branchError, /closed/);
  assert.equal(api.store.getState().messages.at(-1).isStreaming,false);
  assert.equal(api.store.getState().messages.at(-1).thinkingStatus,"interrupted");
});
