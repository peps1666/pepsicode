const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const ts = require("typescript");

// Compile the pure view model with the client's existing compiler.
const source = readFileSync(join(__dirname, "../src/conversation/toolActivity.ts"), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
const viewModel = {};
new Function("exports", compiled.outputText)(viewModel);
const { buildToolActivities, describeTool } = viewModel;

const message = (id, role, toolName, extra = {}) => ({ id, role, toolName, content: "", timestamp: 0, ...extra });

test("pairs interleaved calls and results without creating duplicate rows", () => {
  const read = message("1", "tool_call", "read_file");
  const run = message("2", "tool_call", "run_command");
  const progress = message("3", "progress", undefined, { content: "Checking files" });
  const result = message("4", "tool_result", "read_file", { content: "file contents" });
  const error = message("5", "tool_result", "run_command", { content: "failed", isError: true });
  const rows = buildToolActivities([read, run, progress, result, error]);
  assert.equal(rows.length, 3);
  assert.equal(rows[0].result, result);
  assert.equal(rows[1].result, error);
  assert.equal(rows[2].message, progress);
});

test("matches repeated same-name calls in arrival order", () => {
  const calls = [message("1", "tool_call", "read_file"), message("2", "tool_call", "read_file")];
  const results = [message("3", "tool_result", "read_file"), message("4", "tool_result", "read_file")];
  const rows = buildToolActivities([...calls, ...results]);
  assert.equal(rows[0].result, results[0]);
  assert.equal(rows[1].result, results[1]);
});

test("retains unmatched results and pending calls", () => {
  const orphan = message("1", "tool_result", "read_file");
  const pending = message("2", "tool_call", "run_command");
  assert.deepEqual(buildToolActivities([orphan, pending]), [{ message: orphan }, { message: pending }]);
});

test("normalizes target text without modifying the original arguments", () => {
  const call = message("1", "tool_call", "run_command", { toolInput: { command: "npm test\n -- --run" } });
  assert.deepEqual(describeTool(call), { label: "Run command", target: "npm test -- --run" });
  assert.equal(call.toolInput.command, "npm test\n -- --run");
  assert.deepEqual(describeTool(message("2", "tool_call", "custom_tool")), { label: "Custom tool", target: "" });
});
