const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const ts = require("typescript");

function projects(saved = "[]") {
  const requests = [];
  const storage = new Map([["pepsicode.projects.v1", saved]]);
  const connection = {
    useConnectionStore: { getState: () => ({ ws: {} }) },
    sendRequest: (_ws, method, params) => new Promise((resolve, reject) => requests.push({ method, params, resolve, reject })),
  };
  const source = readFileSync(join(__dirname, "../src/stores/projects.ts"), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const api = {};
  new Function("require", "exports", "localStorage", compiled)(
    (id) => id === "./connection" ? connection : require(id), api,
    { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
  );
  return { ...api, store: api.useProjectsStore, requests, storage };
}

test("project identity handles Windows separators and casing without merging distinct POSIX paths", () => {
  const api = projects();
  assert.equal(api.projectKey("E:\\Code\\Demo\\"), api.projectKey("e:/code/demo"));
  assert.equal(api.projectKey("\\\\Server\\Share\\Demo"), api.projectKey("//server/share/demo/"));
  assert.notEqual(api.projectKey("/work/Demo"), api.projectKey("/work/demo"));
});

test("added projects and their collapsed state survive a reload without duplicates", () => {
  const api = projects();
  api.store.getState().rememberProject("E:\\Code\\Demo");
  api.store.getState().rememberProject("e:/code/demo/");
  api.store.getState().rememberProject("E:/Code/Other");
  api.store.getState().setExpanded("e:/code/demo", false);
  const reloaded = projects(api.storage.get("pepsicode.projects.v1"));
  assert.deepEqual(reloaded.store.getState().projects, [
    { path: "E:\\Code\\Demo", expanded: false }, { path: "E:/Code/Other", expanded: true },
  ]);
  assert.deepEqual(reloaded.store.getState().byProject, {});
});

test("invalid saved project data does not break the sidebar", () => {
  for (const value of ["invalid-json", "null", "{}", '[null, {}, {"path": 2}]']) {
    assert.deepEqual(projects(value).store.getState().projects, []);
  }
});

test("parallel workspace queries keep conversations in their own project", async () => {
  const api = projects();
  const first = api.store.getState().loadSessions("/alpha");
  const second = api.store.getState().loadSessions("/beta");
  assert.deepEqual(api.requests.map((request) => request.params), [{ workspace: "/alpha" }, { workspace: "/beta" }]);
  api.requests[1].resolve({ sessions: [{ session_id: "beta-chat" }] });
  await second;
  api.requests[0].resolve({ sessions: [{ session_id: "alpha-chat" }] });
  await first;
  assert.equal(api.store.getState().byProject["/alpha"].sessions[0].session_id, "alpha-chat");
  assert.equal(api.store.getState().byProject["/beta"].sessions[0].session_id, "beta-chat");
});

test("a late sidebar response cannot overwrite the live conversation list", async () => {
  const api = projects();
  const pending = api.store.getState().loadSessions("/alpha");
  api.store.getState().syncSessions("/alpha", [{ session_id: "just-saved" }]);
  api.requests[0].resolve({ sessions: [{ session_id: "outdated" }] });
  await pending;
  assert.equal(api.store.getState().byProject["/alpha"].sessions[0].session_id, "just-saved");
  assert.equal(api.store.getState().byProject["/alpha"].loading, false);
});

test("duplicate loads share a query, and failed loads can be retried", async () => {
  const api = projects();
  const first = api.store.getState().loadSessions("/alpha");
  await api.store.getState().loadSessions("/alpha");
  assert.equal(api.requests.length, 1);
  api.requests[0].reject(new Error("offline"));
  await first;
  assert.equal(api.store.getState().byProject["/alpha"].error, "offline");
  const retry = api.store.getState().loadSessions("/alpha");
  api.requests[1].resolve({ sessions: [] });
  await retry;
  assert.equal(api.store.getState().byProject["/alpha"].error, "");
});
