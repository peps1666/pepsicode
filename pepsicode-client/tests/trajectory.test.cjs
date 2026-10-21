const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
function load(name) {
  const source = fs.readFileSync(path.join(__dirname, `../src/conversation/${name}.ts`), "utf8");
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const result = {};
  new Function("require", "exports", js)(id => id === "./toolActivity" ? load("toolActivity") : require(id), result);
  return result;
}
const { buildTrajectory, formatDuration, verificationSummary } = load("trajectory");
const msg = (id, role, extra = {}) => ({ id, role, content: "", timestamp: 0, ...extra });

test("trajectory keeps failed and subsequent successful calls, timing and per-turn verification", () => {
  const report = { status: "not_run", reason_code: "no_checks", reason: "No verification commands found; result is unverified", ok: false };
  const rows = buildTrajectory([
    msg("1", "user", { content: "Inspect" }),
    msg("2", "tool_call", { callId: "first", toolName: "read_file", toolInput: { path: "missing" } }),
    msg("3", "tool_result", { callId: "first", toolName: "read_file", isError: true, content: "File not found", startedAt: 100, durationMs: 24 }),
    msg("4", "tool_call", { callId: "second", toolName: "read_file", toolInput: { path: "README" } }),
    msg("5", "tool_result", { callId: "second", toolName: "read_file", content: "docs", durationMs: 8 }),
    msg("6", "assistant", { content: "Done", verification: report }),
    msg("7", "user", { content: "Another task" }),
    msg("8", "assistant", { content: "Answer" }),
  ], false);
  const calls = rows.filter(row => row.kind === "tool");
  assert.deepEqual(calls.map(row => row.status), ["failed", "completed"]);
  assert.equal(calls[0].durationMs, 24);
  assert.equal(calls[0].output, "File not found");
  assert.equal(rows.filter(row => row.kind === "verification").length, 1);
  assert.equal(rows.find(row => row.kind === "verification").turn, 1);
  assert.equal(rows.find(row => row.kind === "verification").status, "not_run");
  assert.ok(!rows.find(row => row.kind === "verification").summary.includes("unverified"));
});

test("historical unknown durations stay unknown; unfinished tools stop spinning at turn end", () => {
  const history = [msg("1", "user"), msg("2", "tool_call", { toolName: "read_file" })];
  assert.equal(buildTrajectory(history, true)[1].status, "running");
  const stopped = buildTrajectory(history, false)[1];
  assert.equal(stopped.status, "cancelled");
  assert.equal(formatDuration(stopped.durationMs), "—");
  assert.equal(formatDuration(0), "0 ms");
  assert.equal(formatDuration(1250), "1.3 s");
});

test("retries are independent records; provider error text and authentication failures remain visible", () => {
  const rows = buildTrajectory([
    msg("1", "user"), msg("2", "progress", { activityKind: "retry", content: "Retry", retry: { attempt: 1, limit: 2, delay_ms: 500, reason: "HTTP 503" } }),
    msg("3", "assistant", { content: "Invalid API Key", isError: true }),
  ], false);
  assert.equal(rows[1].kind, "retry");
  assert.equal(rows[1].retry.attempt, 1);
  assert.equal(rows[2].status, "failed");
  assert.equal(rows[2].output, "Invalid API Key");
  assert.equal(verificationSummary({status:"passed",reason:"1 check passed",ok:true}), "项目检查通过");
});
