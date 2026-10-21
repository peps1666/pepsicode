const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const ts = require("typescript");
const React = require("react");
const { create, act } = require("react-test-renderer");

function load(name) {
  const source = readFileSync(resolve(__dirname, `../src/components/${name}.tsx`), "utf8");
  const js = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  const result = {};
  new Function("require", "exports", js)(id => id === "./Icon" ? { default: load("Icon"), __esModule: true } : id.endsWith(".css") ? { default: {} } : require(id), result);
  return result.default;
}

const Select = load("Select");
const options = [{ value: "a", label: "Alpha" }, { value: "b", label: "Beta", disabled: true }, { value: "c", label: "Charlie" }];
global.document = { addEventListener() {}, removeEventListener() {} };
function key(renderer, name) {
  const event = { key: name, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
  act(() => renderer.root.findByProps({ role: "combobox" }).props.onKeyDown(event));
  return event;
}

test("keyboard navigation skips disabled entries and commits without submitting the form", () => {
  let renderer, selected;
  act(() => { renderer = create(React.createElement(Select, { label: "Model", value: "a", options, onChange: value => { selected = value; } })); });
  assert.equal(renderer.root.findByType("button").props.type, "button");
  assert.equal(key(renderer, "ArrowDown").prevented, true);
  key(renderer, "ArrowDown");
  assert.match(renderer.root.findByProps({ role: "combobox" }).props["aria-activedescendant"], /-2$/);
  assert.equal(key(renderer, "Enter").prevented, true);
  assert.equal(selected, "c");
  assert.equal(renderer.root.findByProps({ role: "combobox" }).props["aria-expanded"], false);
  act(() => renderer.unmount());
});

test("Escape dismisses the menu without closing its dialog; Tab dismisses and preserves focus navigation", () => {
  let renderer;
  act(() => { renderer = create(React.createElement(Select, { label: "Provider", value: "a", options, onChange() {} })); });
  key(renderer, "Enter");
  const escape = key(renderer, "Escape");
  assert.equal(escape.prevented, true);
  assert.equal(escape.stopped, true);
  key(renderer, "Enter");
  assert.ok(!key(renderer, "Tab").prevented);
  assert.equal(renderer.root.findByProps({ role: "combobox" }).props["aria-expanded"], false);
  act(() => renderer.unmount());
});

test("Home, End and typing navigate options; disabled controls stay closed", () => {
  let renderer;
  const props = { label: "Provider", value: "a", options, onChange() {} };
  act(() => { renderer = create(React.createElement(Select, props)); });
  key(renderer, "Enter"); key(renderer, "End");
  assert.match(renderer.root.findByProps({ role: "combobox" }).props["aria-activedescendant"], /-2$/);
  key(renderer, "Home");
  assert.match(renderer.root.findByProps({ role: "combobox" }).props["aria-activedescendant"], /-0$/);
  key(renderer, "c");
  assert.match(renderer.root.findByProps({ role: "combobox" }).props["aria-activedescendant"], /-2$/);
  act(() => renderer.update(React.createElement(Select, { ...props, disabled: true })));
  key(renderer, "Enter");
  assert.equal(renderer.root.findByProps({ role: "combobox" }).props["aria-expanded"], false);
  act(() => renderer.update(React.createElement(Select, props)));
  assert.equal(renderer.root.findByProps({ role: "combobox" }).props["aria-expanded"], false);
  act(() => renderer.unmount());
});
