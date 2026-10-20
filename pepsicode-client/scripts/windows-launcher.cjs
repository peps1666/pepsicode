const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

module.exports = async function afterPack(context) {
  const stage = path.resolve(context.appOutDir);
  const launcher = path.join(stage, "Pepsicode.exe");
  const desktop = path.join(stage, "PepsicodeDesktop.exe");
  // Rename only the generated executable inside this exact build directory.
  if (path.dirname(launcher) !== stage || path.dirname(desktop) !== stage) throw new Error("Invalid executable staging path");
  fs.renameSync(launcher, desktop);
  const compiler = path.join(process.env.SystemRoot || "C:\\Windows", "Microsoft.NET/Framework64/v4.0.30319/csc.exe");
  const icon = path.resolve(__dirname, "../assets/app-icon.ico");
  const { rcedit } = await import("rcedit");
  await rcedit(desktop, { icon, "file-version": context.packager.appInfo.version,
    "product-version": context.packager.appInfo.version,
    "version-string": { ProductName: "Pepsicode", FileDescription: "Pepsicode Desktop" } });
  execFileSync(compiler, ["/nologo", "/target:winexe", "/platform:x64", "/codepage:65001", "/reference:System.Windows.Forms.dll",
    `/win32icon:${icon}`,
    `/out:${launcher}`, path.resolve(__dirname, "../../packaging/windows-launcher.cs")], { windowsHide: true, stdio: "inherit" });
};
