const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const ts = require("typescript");
const output = ts.transpileModule(fs.readFileSync(path.join(__dirname, "../electron/build-info.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const api = {};
new Function("exports", "require", output)(api, require);

test("packaged about identity comes from its own manifest and rejects a mismatched version", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pepsi-identity-"));
  try {
    const options = { packaged: true, resourcesPath: root, projectRoot: root, version: "2.0.7" };
    assert.throws(() => api.buildInformation(options));
    const manifest = { schema_version: 1, version: "2.0.7", git_sha: "a".repeat(40), dirty: true, channel: "development" };
    fs.writeFileSync(path.join(root, "build-info.json"), JSON.stringify(manifest));
    assert.deepEqual(api.buildInformation(options), manifest);
    assert.match(api.describeBuild(manifest), /包含未提交修改/);
    assert.throws(() => api.buildInformation({ ...options, version: "2.0.8" }), /版本不一致/);
  } finally {
    // This test owns the exact directory returned by mkdtemp.
    fs.rmSync(root, { recursive: true, force: true });
  }
});
