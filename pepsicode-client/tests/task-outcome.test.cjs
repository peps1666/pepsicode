const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), ts = require("typescript");
const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../src/conversation/taskOutcome.ts"), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const exported = {};
new Function("exports", code)(exported);
const { taskOutcome, satisfiedCount } = exported;

test("artifact proof is qualified and stale or mixed evidence is never counted", () => {
  const task = { id: "task", revision: 2, status: "complete", assurance: "artifact_checked", criteria: [{ id: "artifact" }],
    acceptance: { valid: true, task_revision: 2, acceptance_run_id: "run", workspace: { digest: "state" } },
    evidence: [{ status: "satisfied", task_revision: 1, acceptance_run_id: "old", workspace_digest: "old" }] };
  assert.equal(taskOutcome(task), "产物已检查，行为未验证");
  assert.equal(satisfiedCount(task), 0);
  task.evidence.push({ status: "satisfied", task_revision: 2, acceptance_run_id: "run", workspace_digest: "state" });
  assert.equal(satisfiedCount(task), 1);
  task.revision += 1;
  assert.equal(taskOutcome(task), "需要重新核对");
  assert.equal(satisfiedCount(task), 0);
});
