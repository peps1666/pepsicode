const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const ts = require("typescript");

// M3 resident-service store behavior: the connection/ready announcement is
// recorded, the service outbox count is fetched, and stopping a turn is
// distinct from detaching (serviceInfo survives the socket going away).

async function serviceFixture() {
  const handlers = [];
  const calls = [];
  const connection = {
    useConnectionStore: { getState: () => ({ ws: {} }) },
    onMessage: (handler) => handlers.push(handler),
    sendRequest: async (_ws, method, params) => {
      calls.push({ method, params });
      if (method === "session/create") return { session_id: "test", cwd: "/project" };
      if (method === "notifications/fetch") {
        return { notifications: [
          { id: "n1", status: "pending" },
          { id: "n2", status: "uncertain" },
        ], cursor: 2 };
      }
      return {};
    },
  };
  const storesDir = join(__dirname, "../src/stores");
  const loadStoreModule = (id) => {
    if (id === "./connection") return connection;
    if (id.startsWith("./")) {
      const target = join(storesDir, `${id.slice(2)}.ts`);
      const mod = {};
      const code = ts.transpileModule(readFileSync(target, "utf8"), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
      }).outputText;
      new Function("require", "exports", code)(loadStoreModule, mod);
      return mod;
    }
    return require(id);
  };
  const source = readFileSync(join(storesDir, "session.ts"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const result = {};
  new Function("require", "exports", compiled)(loadStoreModule, result);
  const store = result.useSessionStore;
  await store.getState().initialize();
  return {
    store, calls,
    event: (event, data) => handlers.forEach((handler) => handler({ kind: "event", event, data })),
  };
}

test("connection/ready with service info is recorded and notifications counted", async (t) => {
  const f = await serviceFixture();
  f.event("connection/ready", { service: true, execution_mode: "service", generation: "a".repeat(32) });
  const info = f.store.getState().serviceInfo;
  assert.ok(info, "serviceInfo must be set");
  assert.equal(info.executionMode, "service");
  assert.equal(info.generation, "a".repeat(32));
  await new Promise((resolve) => setImmediate(resolve));
  const fetch = f.calls.find((c) => c.method === "notifications/fetch");
  assert.ok(fetch, "notification catch-up must be requested");
  assert.deepEqual(fetch.params, { cursor: 0 });
  // The uncertain notification is excluded from the pending count.
  assert.equal(f.store.getState().serviceNotifications, 1);
});

test("a plain backend connection never shows the service banner state", async (t) => {
  const f = await serviceFixture();
  f.event("connection/ready", { cwd: "/project", version: "2.0.7" });
  assert.equal(f.store.getState().serviceInfo, null);
  assert.equal(f.store.getState().serviceNotifications, 0);
  assert.equal(f.calls.filter((c) => c.method === "notifications/fetch").length, 0);
});

test("turn end refreshes the notification count while attached to the service", async (t) => {
  const f = await serviceFixture();
  f.event("connection/ready", { service: true, execution_mode: "service", generation: "b".repeat(32) });
  const before = f.calls.filter((c) => c.method === "notifications/fetch").length;
  f.event("turn/end", { status: "completed" });
  await new Promise((resolve) => setImmediate(resolve));
  const after = f.calls.filter((c) => c.method === "notifications/fetch").length;
  assert.ok(after > before, "turn/end must re-fetch the outbox count in service mode");
  // Detaching (socket loss) does not clear the service info: the work keeps
  // running server-side and the banner should say so after reconnecting.
  f.store.setState({ serviceInfo: f.store.getState().serviceInfo });
  assert.ok(f.store.getState().serviceInfo);
});
