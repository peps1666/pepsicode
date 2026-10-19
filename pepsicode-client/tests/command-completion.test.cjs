const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const ts = require("typescript");

const source = readFileSync(join(__dirname, "../src/conversation/commandCompletion.ts"), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
const api = {};
new Function("exports", compiled.outputText)(api);
const commands = [
  { name: "/model", usage: "/model", description: "Select model", source: "builtin" },
  { name: "/model", usage: "/model list", description: "List models", source: "builtin" },
  { name: "/review", usage: "/review [focus]", description: "Review changes", source: "project" },
];

test("completion matches command names and deduplicates builtin variants", () => {
  assert.deepEqual(api.matchingCommands(commands, "/").map((command) => command.name), ["/model", "/review"]);
  assert.deepEqual(api.matchingCommands(commands, "/rev"), [commands[2]]);
  assert.deepEqual(api.matchingCommands(commands, "/missing"), []);
});

test("arguments and ordinary prompts do not open or capture the completion menu", () => {
  for (const input of ["ordinary prompt", "/review ", "/review 2", '/review "two words"']) {
    assert.deepEqual(api.matchingCommands(commands, input), []);
  }
});

test("accepting a completion inserts only the invocation, not placeholder text", () => {
  assert.equal(api.commandDraft(commands[2]), "/review ");
  assert.equal(api.commandDraft(commands[0]), "/model");
});
