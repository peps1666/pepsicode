const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");

function load(name) {
  const source = fs.readFileSync(path.join(__dirname, `../electron/${name}.ts`), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const api = {};
  new Function("exports", "require", output)(api, (id) => id === "./server-runtime" ? load("server-runtime") : require(id));
  return api;
}

const runtime = load("server-runtime");

test("packaged launch uses the bundled backend even when a system Python override is present", () => {
  const launch = runtime.backendLaunch({ packaged: true, resourcesPath: "C:/中文 App/resources", projectRoot: "C:/source", workspace: "C:/项目 folder", env: { PEPSI_PYTHON: "unavailable-python" }, exists: () => true });
  assert.equal(launch.command, path.join("C:/中文 App/resources", "backend", "pepsicode-server.exe"));
  assert.equal(launch.cwd, "C:/项目 folder");
  assert.deepEqual(launch.args, []);
});

test("missing bundled backend fails explicitly instead of falling back to Python", () => {
  assert.throws(() => runtime.backendLaunch({ packaged: true, resourcesPath: "C:/app/resources", projectRoot: "C:/source", workspace: "C:/work", env: {}, exists: () => false }), /完整解压/);
});

test("development launch preserves the selected Python executable as a single path", () => {
  const launch = runtime.backendLaunch({ packaged: false, resourcesPath: "unused", projectRoot: "C:/source", workspace: "C:/source", env: { PEPSI_PYTHON: "C:/Program Files/Python/python.exe" } });
  assert.equal(launch.command, "C:/Program Files/Python/python.exe");
  assert.deepEqual(launch.args, ["-m", "pepsicode.server"]);
});

test("packaged environment removes Python injection settings and preserves provider credentials", () => {
  const env = runtime.pythonEnvironment({ PYTHONHOME: "bad", PYTHONPATH: "bad", PYTHONSTARTUP: "bad", TEAM_API_KEY: "test" }, true);
  assert.equal(env.PYTHONHOME, undefined);
  assert.equal(env.PYTHONPATH, undefined);
  assert.equal(env.PYTHONSTARTUP, undefined);
  assert.equal(env.TEAM_API_KEY, "test");
});

test("an isolated desktop data directory also isolates the backend home without changing the GUI's native profile", () => {
  const source = { USERPROFILE: "C:/Users/real-user", HOME: "C:/Users/real-user", PEPSI_CODE_DESKTOP_DATA_DIR: "C:/temporary-profile" };
  const env = runtime.pythonEnvironment(source, true);
  assert.equal(env.USERPROFILE, path.join("C:/temporary-profile", "backend-home"));
  assert.equal(env.HOME, env.USERPROFILE);
  assert.equal(source.USERPROFILE, "C:/Users/real-user");
});

test("backend readiness accepts a split startup marker and stop reaps the owned child", async () => {
  const api = load("backend-process");
  const child = await api.launchBackend({ command: process.execPath, args: ["-e", "process.stdout.write('pepsicode server list'); setTimeout(() => console.log('ening on ws://127.0.0.1:9876'), 20); setInterval(() => {}, 1000)", "--"], cwd: process.cwd() }, { port: 9876, token: "private-test-token", origins: ["null"], timeoutMs: 3000 });
  assert.equal(child.exitCode, null);
  await api.stopBackend(child);
  assert.ok(child.exitCode !== null || child.signalCode !== null);
});

test("an early exit fails startup with its exit code", async () => {
  const api = load("backend-process");
  await assert.rejects(api.launchBackend({ command: process.execPath, args: ["-e", "process.exit(7)", "--"], cwd: process.cwd() }, { port: 9876, token: "test", origins: [], timeoutMs: 3000 }), /7/);
});

test("a missing executable is reported without an unhandled child-process error", async () => {
  const api = load("backend-process");
  await assert.rejects(api.launchBackend({ command: path.join(__dirname, "missing-backend.exe"), args: [], cwd: process.cwd() }, { port: 9876, token: "test", origins: [], timeoutMs: 1000 }), /ENOENT/);
});

test("a process that never announces readiness times out and is stopped", async () => {
  const api = load("backend-process");
  await assert.rejects(api.launchBackend({ command: process.execPath, args: ["-e", "setInterval(() => {}, 1000)", "--"], cwd: process.cwd() }, { port: 9876, token: "test", origins: [], timeoutMs: 150 }), /超时/);
});
