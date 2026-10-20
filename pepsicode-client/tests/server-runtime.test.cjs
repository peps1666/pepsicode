const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const ts = require("typescript");
const source = readFileSync(join(__dirname, "../electron/server-runtime.ts"), "utf8");
const output = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const api = {};
new Function("exports", "require", output)(api, require);

test("Python inherits arbitrary provider credentials and settings without Electron runtime flags", () => {
  const source = {ANTHROPIC_API_KEY: "dummy", MOONSHOT_API_KEY: "dummy", TEAM_CUSTOM_KEY: "dummy", PEPSI_CODE_PROVIDER: "kimi", PEPSI_CODE_MODEL: "custom-model", PATH: "python-path", ELECTRON_RUN_AS_NODE: "1", CHROME_CRASHPAD_PIPE_NAME: "bad", NODE_OPTIONS: "--require injected"};
  const environment = api.pythonEnvironment(source);
  for (const key of ["ANTHROPIC_API_KEY", "MOONSHOT_API_KEY", "TEAM_CUSTOM_KEY", "PEPSI_CODE_PROVIDER", "PEPSI_CODE_MODEL", "PATH"]) assert.equal(environment[key], source[key]);
  for (const key of ["ELECTRON_RUN_AS_NODE", "CHROME_CRASHPAD_PIPE_NAME", "NODE_OPTIONS"]) assert.equal(environment[key], undefined);
  assert.equal(source.ELECTRON_RUN_AS_NODE, "1");
});

test("only the packaged renderer or configured dev origin may request the connection credential", () => {
  assert.ok(api.trustedRenderer("file:///app/dist/index.html#chat", "file:///app/dist/index.html"));
  assert.ok(api.trustedRenderer("http://localhost:5173/chat", "http://localhost:5173"));
  assert.ok(!api.trustedRenderer("file:///app/other.html", "file:///app/dist/index.html"));
  assert.ok(!api.trustedRenderer("https://untrusted.example", "file:///app/dist/index.html"));
  assert.ok(!api.trustedRenderer("http://localhost:5174", "http://localhost:5173"));
});

test("server origins follow the renderer URL instead of a fixed development port", () => {
  assert.deepEqual(api.serverOrigins("http://127.0.0.1:6248/chat"), ["null", "file://", "http://127.0.0.1:6248"]);
  assert.deepEqual(api.serverOrigins("https://localhost:7341/chat"), ["null", "file://", "https://localhost:7341"]);
  assert.deepEqual(api.serverOrigins("file:///app/dist/index.html"), ["null", "file://"]);
  assert.ok(!api.trustedRenderer("http://localhost:5173", "http://127.0.0.1:6248/chat"));
});
