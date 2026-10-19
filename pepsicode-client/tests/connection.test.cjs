const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const ts = require("typescript");

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

test("long verification turns do not expire after 60 seconds", async () => {
  const api = connection();
  const connected = api.useConnectionStore.getState().connect(8765);
  const ws = api.sockets[0];
  ws.onopen();
  await connected;
  const response = api.sendRequest(ws, "turn/run", { input: "fix and verify" });
  assert.equal(api.timers.filter(timer => timer.active && timer.ms === 60000).length, 0);
  ws.onmessage({ data: JSON.stringify({ kind: "response", id: ws.sent[0].id, result: { status: "completed" } }) });
  assert.deepEqual(await response, { status: "completed" });
});

test("ordinary requests retain timeout and clear it after a response", async () => {
  const api = connection();
  const connected = api.useConnectionStore.getState().connect(8765);
  const ws = api.sockets[0];
  ws.onopen(); await connected;
  const response = api.sendRequest(ws, "status");
  const timer = api.timers.find(timer => timer.active && timer.ms === 60000);
  assert.ok(timer);
  ws.onmessage({ data: JSON.stringify({ kind: "response", id: ws.sent[0].id, result: "ok" }) });
  assert.equal(await response, "ok");
  assert.equal(timer.active, false);
});

test("socket close rejects pending long turns and releases timers", async () => {
  const api = connection();
  const connected = api.useConnectionStore.getState().connect(8765);
  const ws = api.sockets[0];
  ws.onopen(); await connected;
  const turn = api.sendRequest(ws, "turn/run");
  const status = api.sendRequest(ws, "status");
  const expectations = [assert.rejects(turn, /connection closed/), assert.rejects(status, /connection closed/)];
  ws.close();
  await Promise.all(expectations);
  assert.equal(api.timers.filter(timer => timer.active).length, 0);
});

test("late close from an old socket does not disconnect its replacement", async () => {
  const api = connection();
  const first = api.useConnectionStore.getState().connect(8765);
  api.sockets[0].onopen(); await first;
  const second = api.useConnectionStore.getState().connect(8766);
  api.sockets[1].onopen(); await second;
  api.sockets[0].close();
  assert.equal(api.useConnectionStore.getState().ws, api.sockets[1]);
  assert.equal(api.useConnectionStore.getState().status, "connected");
});
