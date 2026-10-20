const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { assertBackend } = require("../scripts/check-backend.cjs");

test("packaging rejects a missing or incomplete backend", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "pepsi-package-"));
  try {
    assert.throws(() => assertBackend(directory), /backend:build/);
    fs.writeFileSync(path.join(directory, "pepsicode-server.exe"), "fixture");
    assert.throws(() => assertBackend(directory), /Python/);
    fs.mkdirSync(path.join(directory, "_internal"));
    fs.writeFileSync(path.join(directory, "_internal", "python312.dll"), "fixture");
    assert.throws(() => assertBackend(directory), /agent/);
    const agents = path.join(directory, "_internal", "pepsicode", "agents", "builtins");
    fs.mkdirSync(agents, { recursive: true });
    for (const name of ["explore", "plan", "general", "verification"]) fs.writeFileSync(path.join(agents, name + ".md"), "fixture");
    assert.doesNotThrow(() => assertBackend(directory));
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
