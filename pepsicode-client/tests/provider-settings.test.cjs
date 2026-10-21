const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const ts = require("typescript");
const React = require("react");
const { create, act } = require("react-test-renderer");

function component(name, state, directory = "conversation") {
  const filename = path.resolve(__dirname, `../src/${directory}/${name}.tsx`);
  const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const result = {};
  const requireLocal = createRequire(filename);
  new Function("require", "exports", code)((id) => {
    if (id === "../stores/session") return { useSessionStore: () => state };
    if (id.endsWith(".css")) return { default: {} };
    if (id === "./ProviderSetup") return { default: component("ProviderSetup", state), __esModule: true };
    if (id.startsWith("../components/") || id === "./Icon") return { default: component(id.split("/").pop(), state, "components"), __esModule: true };
    return requireLocal(id);
  }, result);
  return result.default;
}

const presets = ["deepseek", "kimi", "glm", "qwen"].map(id => ({ id, name: id, configured: false, baseUrl: `https://${id}.example/anthropic` }));
function state(overrides = {}) {
  return { providers: presets, provider: "", model: "mock", models: [], reasoningEffort: "default", isMock: true,
    loadProviderModels: async () => { throw new Error("unconfigured catalogs must not load"); },
    cancelProviderSetup: async () => {}, ...overrides };
}
const submit = async root => act(async () => root.findByType("form").props.onSubmit({ preventDefault() {} }));
const edit = (root, name, value) => act(() => {
  const customSelect = root.findAll(node => typeof node.type === "function" && node.props.label === name)[0];
  if (customSelect) customSelect.props.onChange(value);
  else root.findByProps({ "aria-label": name }).props.onChange({ target: { value } });
});

test("first run allows selecting every built-in provider and opens its API key editor", async () => {
  for (const preset of presets) {
    let renderer;
    await act(async () => { renderer = create(React.createElement(component("ModelSettings", state()), { onClose() {} })); });
    const select = renderer.root.findByProps({ "aria-label": "服务商" });
    assert.ok(!select.props.disabled);
    assert.ok(!renderer.root.findByProps({ label: "服务商" }).props.options.find(item => item.value === preset.id).disabled);
    edit(renderer.root, "服务商", preset.id);
    assert.equal(renderer.root.findByProps({ label: "配置服务商" }).props.value, preset.id);
    assert.equal(renderer.root.findByProps({ "aria-label": "API Key" }).props.type, "password");
    await act(async () => renderer.unmount());
  }
});

test("built-in setup keeps its identity, confirms before saving and never renders its key", async () => {
  const calls = [];
  const store = state({ probeProvider: async draft => {
    calls.push(["probe", draft]);
    return { token: "setup-token", models: [{ id: "model-b", name: "Model B" }], verified: true, catalogError: "" };
  }, saveProvider: async (...args) => calls.push(["save", ...args]) });
  let renderer, done = false;
  await act(async () => { renderer = create(React.createElement(component("ProviderSetup", store), { initialProvider: "deepseek", onDone() { done = true; }, onCancel() {} })); });
  edit(renderer.root, "API Key", "test-local-only-key");
  await submit(renderer.root);
  assert.equal(calls[0][1].provider, "deepseek");
  assert.equal(calls[0][1].baseUrl, presets[0].baseUrl);
  assert.equal(calls[0][1].apiKey, "test-local-only-key");
  assert.equal(calls.length, 1);
  edit(renderer.root, "选择模型", "model-b");
  await submit(renderer.root);
  assert.ok(!JSON.stringify(renderer.toJSON()).includes("test-local-only-key"));
  assert.equal(calls.length, 1);
  await submit(renderer.root);
  assert.deepEqual(calls[1], ["save", "setup-token", "model-b"]);
  assert.ok(done);
  await act(async () => renderer.unmount());
});

test("custom setup and model discovery failure retain manual model ID entry", async () => {
  let renderer;
  const store = state({ probeProvider: async () => ({ token: "manual", models: [], verified: false, catalogError: "No catalog" }) });
  await act(async () => { renderer = create(React.createElement(component("ProviderSetup", store), { initialProvider: "custom", onDone() {}, onCancel() {} })); });
  edit(renderer.root, "厂商名称", "Local Gateway");
  edit(renderer.root, "API 地址", "http://127.0.0.1:8888/anthropic");
  edit(renderer.root, "API Key", "test-key");
  await submit(renderer.root);
  assert.equal(renderer.root.findByProps({ "aria-label": "选择模型" }).type, "input");
  await act(async () => renderer.unmount());
});

test("changing provider clears the previous key and cancel never saves configuration", async () => {
  let renderer, cancelled = 0, cleaned = 0;
  const store = state({ saveProvider: async () => assert.fail("cancel must not save"), cancelProviderSetup: async () => cleaned++ });
  await act(async () => { renderer = create(React.createElement(component("ProviderSetup", store), { initialProvider: "deepseek", onDone() {}, onCancel() { cancelled++; } })); });
  edit(renderer.root, "API Key", "first-provider-key");
  edit(renderer.root, "配置服务商", "kimi");
  assert.equal(renderer.root.findByProps({ "aria-label": "API Key" }).props.value, "");
  assert.equal(renderer.root.findByProps({ "aria-label": "API 地址" }).props.value, presets[1].baseUrl);
  act(() => renderer.root.findAllByType("button").find(button => button.children.includes("取消")).props.onClick());
  assert.equal(cancelled, 1);
  await act(async () => renderer.unmount());
  assert.equal(cleaned, 1);
});
