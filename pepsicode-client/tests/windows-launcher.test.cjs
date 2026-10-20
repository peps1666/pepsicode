const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync, spawnSync } = require("node:child_process");

test("Windows launcher forwards Unicode, quotes and trailing slashes without a shell and returns the child status", { skip: process.platform !== "win32" }, () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "pepsi launcher "));
  try {
    const compiler = path.join(process.env.SystemRoot || "C:\\Windows", "Microsoft.NET/Framework64/v4.0.30319/csc.exe");
    const launcher = path.join(directory, "Pepsicode.exe");
    const child = path.join(directory, "PepsicodeDesktop.exe");
    const source = path.join(directory, "fixture.cs");
    const output = path.join(directory, "arguments.txt");
    fs.writeFileSync(source, 'using System; using System.IO; class Fixture { static int Main(string[] args) { File.WriteAllLines(Environment.GetEnvironmentVariable("PEPSI_LAUNCHER_TEST_OUTPUT"), args); return 7; } }');
    execFileSync(compiler, ["/nologo", "/target:winexe", "/platform:x64", "/codepage:65001", "/reference:System.Windows.Forms.dll", `/out:${launcher}`, path.resolve(__dirname, "../../packaging/windows-launcher.cs")], { windowsHide: true });
    execFileSync(compiler, ["/nologo", "/target:exe", "/platform:x64", `/out:${child}`, source], { windowsHide: true });
    const args = ["C:\\项目 folder\\", 'a"b', "", "--remote-debugging-port=1234", "a&b"];
    const result = spawnSync(launcher, args, { env: { ...process.env, PEPSI_LAUNCHER_TEST_OUTPUT: output }, windowsHide: true });
    assert.equal(result.status, 7);
    const actual = fs.readFileSync(output, "utf8").replace(/^\uFEFF/, "").replace(/\r/g, "").split("\n");
    actual.pop();
    assert.deepEqual(actual, ["--no-sandbox", ...args]);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
