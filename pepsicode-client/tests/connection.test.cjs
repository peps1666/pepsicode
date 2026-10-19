const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const ts = require("typescript");

function authenticate(ws) {
  ws.onopen();
  ws.onmessage({data: JSON.stringify({kind: "response", id: 0, result: {authenticated: true}})});
}

function connection() {
  const timers = [];
  const sockets = [];
  class Socket {
    static OPEN = 1;
    readyState = 1;
    sent = [];
    constructor() { sockets.push(this); }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() { this.readyState = 3; this.onclose?.(); }
  }
  const source = readFileSync(join(__dirname, "../src/stores/connection.ts"), "utf8");
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const result = {};
  new Function("require", "exports", "WebSocket", "setTimeout", "clearTimeout", js)(
    require, result, Socket,
    (fn, ms) => { const timer = { fn, ms, active: true }; timers.push(timer); return timer; },
    (timer) => { timer.active = false; }
  );
  return { ...result, timers, sockets, Socket };
}

for (const method of ["turn/run", "turn/regenerate", "extension/long-task"]) test(`${method} accepts an explicit unlimited lifecycle`, async () => {
  const api = connection();
  const connected = api.useConnectionStore.getState().connect(8765, "test-token");
  const ws = api.sockets[0];
  authenticate(ws);
  await connected;
  const response = api.sendRequest(ws, method, { input: "fix and verify" }, { timeoutMs: null });
  assert.equal(api.timers.filter(timer => timer.active && timer.ms === 60000).length, 0);
  ws.onmessage({ data: JSON.stringify({ kind: "response", id: ws.sent.at(-1).id, result: { status: "completed" } }) });
  assert.deepEqual(await response, { status: "completed" });
});

test("ordinary requests retain timeout and clear it after a response", async () => {
  const api = connection();
  const connected = api.useConnectionStore.getState().connect(8765, "test-token");
  const ws = api.sockets[0];
  authenticate(ws); await connected;
  const response = api.sendRequest(ws, "status");
  const timer = api.timers.find(timer => timer.active && timer.ms === 60000);
  assert.ok(timer);
  ws.onmessage({ data: JSON.stringify({ kind: "response", id: ws.sent.at(-1).id, result: "ok" }) });
  assert.equal(await response, "ok");
  assert.equal(timer.active, false);
});

test("a caller can choose a deadline without method-name rules", async () => {
  const api = connection();
  const connected = api.useConnectionStore.getState().connect(8765, "test-token");
  const ws = api.sockets[0];
  authenticate(ws); await connected;
  const response = api.sendRequest(ws, "turn/run", {}, { timeoutMs: 1234 });
  const timer = api.timers.find(timer => timer.active && timer.ms === 1234);
  assert.ok(timer);
  ws.onmessage({ data: JSON.stringify({ kind: "response", id: ws.sent.at(-1).id, result: "done" }) });
  assert.equal(await response, "done");
  assert.equal(timer.active, false);
});

test("socket close rejects pending long turns and releases timers", async () => {
  const api = connection();
  const connected = api.useConnectionStore.getState().connect(8765, "test-token");
  const ws = api.sockets[0];
  authenticate(ws); await connected;
  const turn = api.sendRequest(ws, "turn/run");
  const status = api.sendRequest(ws, "status");
  const expectations = [assert.rejects(turn, /connection closed/), assert.rejects(status, /connection closed/)];
  ws.close();
  await Promise.all(expectations);
  assert.equal(api.timers.filter(timer => timer.active).length, 0);
});

test("late close from an old socket does not disconnect its replacement", async () => {
  const api = connection();
  const first = api.useConnectionStore.getState().connect(8765, "test-token");
  authenticate(api.sockets[0]); await first;
  const second = api.useConnectionStore.getState().connect(8766, "test-token");
  authenticate(api.sockets[1]); await second;
  api.sockets[0].close();
  assert.equal(api.useConnectionStore.getState().ws, api.sockets[1]);
  assert.equal(api.useConnectionStore.getState().status, "connected");
});

test("authentication completes before workspace requests become available", async () => {
  const api = connection();
  const ready = api.useConnectionStore.getState().connect(8765, "private-token");
  const ws = api.sockets[0];
  ws.onopen();
  assert.equal(api.useConnectionStore.getState().status, "connecting");
  assert.equal(api.useConnectionStore.getState().ws, null);
  assert.deepEqual(ws.sent[0], {kind: "request", id: 0, method: "connection/auth", params: {token: "private-token"}});
  ws.onmessage({data: JSON.stringify({kind: "response", id: 0, result: {authenticated: true}})});
  await ready;
  assert.equal(api.useConnectionStore.getState().status, "connected");
});

test("replacing a socket rejects its pending turn and ignores stale events and errors", async () => {
  const api = connection();
  const first = api.useConnectionStore.getState().connect(8765, "first");
  authenticate(api.sockets[0]); await first;
  const turn = api.sendRequest(api.sockets[0], "turn/run");
  const rejected = assert.rejects(turn, /closed/);
  const second = api.useConnectionStore.getState().connect(8766, "second");
  authenticate(api.sockets[1]); await second; await rejected;
  const messages = [];
  api.onMessage(message => messages.push(message));
  api.sockets[0].onmessage({data: JSON.stringify({kind: "event", event: "turn/end"})});
  api.sockets[0].onerror();
  api.sockets[0].onopen();
  assert.equal(messages.length, 0);
  assert.equal(api.useConnectionStore.getState().ws, api.sockets[1]);
  assert.equal(api.useConnectionStore.getState().error, null);
});

test("timeout closes the unauthenticated socket and late open cannot revive it", async () => {
  const api = connection();
  const ready = api.useConnectionStore.getState().connect(8765, "token");
  const rejected = assert.rejects(ready, /timed out/);
  api.timers.find(timer => timer.ms === 10000).fn();
  await rejected;
  api.sockets[0].onopen();
  assert.equal(api.sockets[0].readyState, 3);
  assert.equal(api.useConnectionStore.getState().status, "error");
  assert.equal(api.sockets[0].sent.length, 0);
});

test("invalid authentication fails without dispatching application events", async () => {
  const api = connection();
  const ready = api.useConnectionStore.getState().connect(8765, "token");
  const rejected = assert.rejects(ready, /authentication failed/);
  api.sockets[0].onopen();
  api.sockets[0].onmessage({data: JSON.stringify({kind: "response", id: 0, error: {message: "denied"}})});
  await rejected;
  assert.equal(api.sockets[0].readyState, 3);
});
