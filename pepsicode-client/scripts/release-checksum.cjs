const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { version } = require("../package.json");
const name = `Pepsicode-${version}-win-x64.zip`;
const file = path.resolve(__dirname, "../release", name);
const hash = crypto.createHash("sha256");
const input = fs.createReadStream(file);
input.on("error", error => { console.error(error.message); process.exitCode = 1; });
input.on("data", chunk => hash.update(chunk));
input.on("end", () => {
  fs.writeFileSync(file + ".sha256", `${hash.digest("hex")}  ${name}\n`);
  console.log(`ZIP: ${file}\nSHA-256: ${file}.sha256`);
});
