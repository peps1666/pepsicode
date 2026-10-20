const fs = require("node:fs");
const path = require("node:path");

function assertBackend(directory) {
  if (!fs.existsSync(path.join(directory, "pepsicode-server.exe"))) throw new Error("Missing backend. Run npm run backend:build first.");
  const internal = path.join(directory, "_internal");
  if (!fs.existsSync(internal) || !fs.readdirSync(internal).some(name => /^python3\d+\.dll$/i.test(name))) throw new Error("Missing bundled Python runtime.");
  for (const name of ["explore", "plan", "general", "verification"]) {
    if (!fs.existsSync(path.join(internal, "pepsicode", "agents", "builtins", name + ".md"))) throw new Error(`Missing built-in agent: ${name}`);
  }
}

module.exports = async (context) => assertBackend(path.resolve(context.packager.projectDir, "../build/desktop-backend/pepsicode-server"));
module.exports.assertBackend = assertBackend;
