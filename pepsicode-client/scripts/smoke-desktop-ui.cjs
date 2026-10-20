// End-to-end smoke against the real ZIP: no mock WebSocket or injected app state.
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const http = require("node:http");
const { spawn, execFileSync } = require("node:child_process");
const assert = require("node:assert/strict");
const { path7za } = require("7zip-bin");
const { version } = require("../package.json");

const client = path.resolve(__dirname, "..");
const build = path.resolve(client, "../build");
const archive = path.join(client, "release", `Pepsicode-${version}-win-x64.zip`);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function until(action, message, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await action();
    if (result) return result;
    await pause(100);
  }
  throw new Error(message);
}

async function main() {
  const directory = fs.mkdtempSync(path.join(build, "UI 验收 space "));
  const application = path.join(directory, "解压后的软件");
  const home = path.join(directory, "isolated-home");
  const userData = path.join(home, "desktop-profile");
  const workspace = path.join(directory, "项目 workspace");
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(userData, { recursive: true });
  execFileSync(path7za, ["x", archive, `-o${application}`, "-y"], { windowsHide: true, stdio: "pipe" });
  const icon = fs.readFileSync(path.join(client, "assets/app-icon.ico"));
  for (const name of ["Pepsicode.exe", "PepsicodeDesktop.exe"]) {
    const executable = fs.readFileSync(path.join(application, name));
    for (let i = 0; i < icon.readUInt16LE(4); i++) {
      const size = icon.readUInt32LE(6 + i * 16 + 8), offset = icon.readUInt32LE(6 + i * 16 + 12);
      assert.ok(executable.includes(icon.subarray(offset, offset + size)), `${name} is missing cat icon size ${i}`);
    }
  }
  let authenticatedCalls = 0;
  const api = http.createServer((request, response) => {
    if (request.headers.authorization !== "Bearer desktop-local-test-key") {
      response.writeHead(401); response.end(); return;
    }
    authenticatedCalls++;
    if (request.method === "GET") {
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ data: [{ id: "desktop-test-model", name: "Desktop Test Model" }] }));
      return;
    }
    request.resume();
    request.on("end", () => {
      response.setHeader("Content-Type", "text/event-stream");
      const events = [
        ["message_start", { message: { usage: { input_tokens: 10 } } }],
        ["content_block_start", { index: 0, content_block: { type: "text", text: "" } }],
        ["content_block_delta", { index: 0, delta: { type: "text_delta", text: "LOCAL_PROVIDER_RESTART_OK" } }],
        ["content_block_stop", { index: 0 }],
        ["message_delta", { delta: { stop_reason: "end_turn" }, usage: { output_tokens: 5 } }],
        ["message_stop", {}],
      ];
      response.end(events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify({ type: event, ...data })}\n\n`).join(""));
    });
  });
  await new Promise(resolve => api.listen(0, "127.0.0.1", resolve));
  const baseUrl = `http://127.0.0.1:${api.address().port}/anthropic`;
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, "127.0.0.1", resolve));
  const debugPort = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(PEPSI_|PYTHON|CONDA|ELECTRON_|NODE_OPTIONS$)/i.test(key) && !/(API_KEY|TOKEN|SECRET)$/i.test(key)));
  Object.assign(env, {
    PATH: path.join(process.env.SystemRoot || "C:\\Windows", "System32"),
    PEPSI_CODE_MODEL_MODE: "mock", PEPSI_CODE_WORKSPACE: workspace,
    PEPSI_CODE_DESKTOP_DATA_DIR: userData, PEPSI_CODE_DESKTOP_SMOKE: "1",
  });
  let processHandle = spawn(path.join(application, "Pepsicode.exe"), [`--remote-debugging-port=${debugPort}`], {
    cwd: directory, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  let socket;
  const log = chunk => { output = (output + chunk.toString()).slice(-12000); };
  processHandle.stdout.on("data", log);
  processHandle.stderr.on("data", log);
  try {
    const target = await until(async () => {
      if (processHandle.exitCode !== null) throw new Error(`Desktop exited (${processHandle.exitCode}): ${output}`);
      try {
        const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
        return targets.find(item => item.type === "page" && item.url.startsWith("file:"));
      } catch { return null; }
    }, "Desktop renderer did not start");
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let nextId = 0;
    const pending = new Map();
    socket.onmessage = event => {
      const message = JSON.parse(event.data);
      if (pending.has(message.id)) {
        const item = pending.get(message.id);
        pending.delete(message.id);
        clearTimeout(item.timer);
        if (message.error) item.reject(new Error(message.error.message)); else item.resolve(message.result);
      }
    };
    socket.onclose = () => {
      for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("Renderer closed")); }
      pending.clear();
    };
    const cdp = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++nextId;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 10000);
      pending.set(id, { resolve, reject, timer });
      socket.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async expression => {
      const response = await cdp("Runtime.evaluate", { expression, returnByValue: true });
      if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
      return response.result.value;
    };
    await until(() => evaluate("!!document.querySelector('#task-composer:not(:disabled)')"), "Desktop did not connect to the bundled backend");
    await evaluate(`(() => {
      const input = document.querySelector('#task-composer');
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, 'ZIP 成品启动验收');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    })()`);
    await until(() => evaluate("!!document.querySelector('button[aria-label=\"发送\"]:not(:disabled)')"), "Send remained disabled");
    await evaluate("document.querySelector('button[aria-label=\"发送\"]').click()");
    await until(() => evaluate("document.body.innerText.includes('This is a minimal pepsicode shell.')"), "Mock response did not reach the real renderer");
    await until(() => evaluate("!!Array.from(document.querySelectorAll('button')).find(b => b.innerText === '配置模型' && !b.disabled)"), "Model settings remained disabled");
    await evaluate("Array.from(document.querySelectorAll('button')).find(b => b.innerText === '配置模型').click()");
    assert.deepEqual(await evaluate("Array.from(document.querySelector('select[aria-label=\"服务商\"]').options).filter(o => ['deepseek','kimi','glm','qwen'].includes(o.value)).map(o => ({id:o.value,disabled:o.disabled}))"),
      ["deepseek", "kimi", "glm", "qwen"].map(id => ({ id, disabled: false })));
    const setValue = async (selector, value, tag = "HTMLInputElement", event = "input") => evaluate(`(() => {
      const input = document.querySelector(${JSON.stringify(selector)});
      Object.getOwnPropertyDescriptor(${tag}.prototype, 'value').set.call(input, ${JSON.stringify(value)});
      input.dispatchEvent(new Event(${JSON.stringify(event)}, { bubbles: true }));
    })()`);
    await setValue('select[aria-label="服务商"]', "deepseek", "HTMLSelectElement", "change");
    assert.equal(await evaluate("document.querySelector('input[aria-label=\"API 地址\"]').value"), "https://api.deepseek.com/anthropic");
    assert.equal(await evaluate("document.querySelector('input[aria-label=\"API Key\"]').type"), "password");
    await setValue('input[aria-label="API Key"]', "desktop-local-test-key");
    await evaluate("document.querySelector('dialog details').open = true");
    await setValue('input[aria-label="API 地址"]', baseUrl);
    await evaluate("document.querySelector('dialog form').requestSubmit()");
    await until(() => evaluate("!!document.querySelector('select[aria-label=\"选择模型\"]')"), "Provider model discovery did not complete");
    await setValue('select[aria-label="选择模型"]', "desktop-test-model", "HTMLSelectElement", "change");
    await evaluate("document.querySelector('dialog form').requestSubmit()");
    assert.equal(await evaluate("document.querySelector('dialog').innerText.includes('desktop-local-test-key')"), false);
    await evaluate("document.querySelector('dialog form').requestSubmit()");
    await until(() => evaluate("!document.querySelector('dialog') && document.body.innerText.includes('Desktop Test Model')"), "Provider save did not activate the selected model");
    const settingsPath = path.join(userData, "backend-home", ".pepsi-code", "settings.json");
    const settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    assert.equal(settings.provider, "deepseek");
    assert.equal(settings.providers.deepseek.apiKey, "desktop-local-test-key");
    assert.equal(settings.model, "desktop-test-model");
    assert.equal(await evaluate("JSON.stringify(localStorage).includes('desktop-local-test-key')"), false);
    assert.equal(fs.readFileSync(path.join(userData, "backend.log"), "utf8").includes("desktop-local-test-key"), false);
    const logText = fs.readFileSync(path.join(userData, "backend.log"), "utf8");
    const port = Number(logText.match(/Starting backend on port (\d+)/)[1]);
    const closing = evaluate("window.close()").catch(() => {});
    await until(() => processHandle.exitCode !== null, "Desktop did not exit cleanly", 15000);
    await closing;
    const listening = await new Promise(resolve => {
      const check = net.connect(port, "127.0.0.1");
      check.once("connect", () => { check.destroy(); resolve(true); });
      check.once("error", () => resolve(false));
    });
    assert.equal(listening, false, "Backend remained alive after closing the desktop");
    const onMessage = socket.onmessage, onClose = socket.onclose;
    socket.close();
    delete env.PEPSI_CODE_MODEL_MODE;
    processHandle = spawn(path.join(application, "Pepsicode.exe"), [`--remote-debugging-port=${debugPort}`], {
      cwd: directory, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
    });
    processHandle.stdout.on("data", log); processHandle.stderr.on("data", log);
    const restarted = await until(async () => {
      if (processHandle.exitCode !== null) throw new Error(`Restart exited: ${processHandle.exitCode}`);
      try { return (await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json()).find(t => t.type === "page" && t.url.startsWith("file:")); }
      catch { return null; }
    }, "Restart renderer did not start");
    socket = new WebSocket(restarted.webSocketDebuggerUrl);
    socket.onmessage = onMessage; socket.onclose = onClose;
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    await until(() => evaluate("!!document.querySelector('#task-composer:not(:disabled)') && document.body.innerText.includes('Desktop Test Model')"), "Saved provider did not survive restart");
    await setValue("#task-composer", "Verify saved local provider", "HTMLTextAreaElement");
    await until(() => evaluate("!!document.querySelector('button[aria-label=\"发送\"]:not(:disabled)')"), "Restart send remained disabled");
    await evaluate("document.querySelector('button[aria-label=\"发送\"]').click()");
    await until(() => evaluate("document.body.innerText.includes('LOCAL_PROVIDER_RESTART_OK')"), "Saved provider failed to perform an authenticated streamed roundtrip");
    assert.ok(authenticatedCalls >= 3);
    const restartPort = Number([...fs.readFileSync(path.join(userData, "backend.log"), "utf8").matchAll(/Starting backend on port (\d+)/g)].at(-1)[1]);
    await evaluate("window.close()").catch(() => {});
    await until(() => processHandle.exitCode !== null, "Restart did not exit cleanly", 15000);
    await until(() => new Promise(resolve => {
      const check = net.connect(restartPort, "127.0.0.1");
      check.once("connect", () => { check.destroy(); resolve(false); }); check.once("error", () => resolve(true));
    }), "Restarted backend remained alive");
    fs.writeFileSync(path.join(build, "desktop-zip-smoke.json"), JSON.stringify({
      passed: true, archive: path.basename(archive), runtimePath: env.PATH,
      sha256: fs.readFileSync(archive + ".sha256", "utf8").split(/\s+/)[0],
      checks: ["real ZIP extraction", "Unicode and space paths", "isolated profile", "no Python/Node on PATH", "real renderer and bundled backend", "mock message roundtrip", "all builtin providers selectable", "DeepSeek key setup and save", "key absent from UI summary, localStorage and logs", "provider persists after restart", "authenticated local provider streaming roundtrip", "cat icon embedded in both executables", "backend shutdown"],
    }, null, 2));
    console.log("PASS: ZIP desktop, cat executable icons, builtin key setup, persistence across restart, authenticated local provider streaming and backend shutdown; no Python/Node on PATH.");
  } catch (error) {
    const logFile = path.join(userData, "backend.log");
    const backendOutput = fs.existsSync(logFile) ? fs.readFileSync(logFile, "utf8") : "No backend log was created";
    throw new Error(`${error.message}\n${output}\n${backendOutput}`);
  } finally {
    socket?.close();
    await new Promise(resolve => api.close(resolve));
    if (processHandle.exitCode === null) {
      execFileSync(path.join(process.env.SystemRoot || "C:\\Windows", "System32/taskkill.exe"), ["/PID", String(processHandle.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
      await until(() => processHandle.exitCode !== null, "Smoke process cleanup failed", 10000);
    }
    // Only remove the exact temporary directory this script just created.
    if (path.dirname(directory) !== build) throw new Error("Unsafe smoke cleanup path");
    fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
