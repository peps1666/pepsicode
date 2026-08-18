import { app, BrowserWindow, ipcMain, dialog } from "electron";
import { spawn, ChildProcess } from "child_process";
import * as path from "path";
import * as net from "net";

// On Windows, Electron helper processes briefly attach to a parent console
// before the main loop takes over, which can show a brief black window.
// These flags reduce the number of helper processes and the chance of
// seeing a console window flash.
if (process.platform === "win32") {
  // --no-sandbox avoids spawning an extra sandbox helper process.
  app.commandLine.appendSwitch("no-sandbox");
  // Stable console output: write Electron logs to stderr rather than
  // opening a log file dialog.  Pairs well with the python server's
  // stdio: ["ignore", "pipe", "pipe"] setup.
  app.commandLine.appendSwitch("enable-logging", "stderr");
  // Bind the AppUserModelID so Windows shows the correct taskbar icon
  // and doesn't fall back to a console window.
  app.setAppUserModelId("com.pepsicode.app");
}

let mainWindow: BrowserWindow | null = null;
let serverProcess: ChildProcess | null = null;
let serverPort = 8765;

function findFreePort(start: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const tryPort = (port: number, attempts: number) => {
      if (attempts <= 0) {
        reject(new Error("Could not find a free port"));
        return;
      }
      const socket = new net.Socket();
      socket.setTimeout(500);
      socket.on("connect", () => {
        // Port is in use, try next
        socket.destroy();
        tryPort(port + 1, attempts - 1);
      });
      socket.on("error", () => {
        // Port is free (connection refused)
        socket.destroy();
        resolve(port);
      });
      socket.on("timeout", () => {
        socket.destroy();
        resolve(port);
      });
      socket.connect(port, "127.0.0.1");
    };
    tryPort(start, 20);
  });
}

async function startPythonServer(port: number): Promise<void> {
  const isDev = !app.isPackaged;
  // Prefer the pepsicode conda env if present, fall back to PATH
  const condaPython = "C:\\Users\\zwsoft\\.conda\\envs\\pepsicode\\python.exe";
  const fs = await import("fs");
  const pythonCmd = process.env.PEPSI_PYTHON || (fs.existsSync(condaPython) ? condaPython : "python");

  const args = ["-m", "pepsicode.server", "--port", String(port), "--log-level", "INFO"];

  // In dev mode, __dirname is dist-electron/, so go up to pepsicode-client/,
  // then up to pepsicode/ (the Python package root).
  // In production, use the packaged resources path.
  const cwd = isDev
    ? path.resolve(__dirname, "..", "..")
    : process.resourcesPath;

  console.log(`[electron] Starting Python server: ${pythonCmd} ${args.join(" ")} (cwd: ${cwd})`);

  // Build a clean environment for the Python child. Electron injects a number
  // of its own variables into process.env (CHROME_*, CRASHPAD pipe names,
  // ELECTRON_*, etc.) that have been observed to destabilise native code in
  // grandchild processes spawned by the Python server (MCP stdio servers via
  // cmd.exe/node), leading to 0xC0000005 access violations that kill the
  // whole server. Pass through only what the Python process actually needs.
  const cleanEnv: NodeJS.ProcessEnv = {};
  const passthrough = [
    "PATH", "PATHEXT", "SystemRoot", "SystemDrive", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "USERNAME",
    "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING", "PYTHONUTF8",
    "PEPSI_PYTHON", "PEPSI_CODE_MODEL_MODE", "LANG", "LC_ALL",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
  ];
  for (const key of passthrough) {
    if (process.env[key] !== undefined) {
      cleanEnv[key] = process.env[key];
    }
  }

  // Suppress Windows GUI assertion pop-ups for the Python child and its
  // grandchildren (MCP stdio servers, cmd.exe).  We do this via
  // ``PYTHONFAULTHANDLER`` so that any crash prints a Python traceback to
  // stderr (which Electron captures) rather than opening a "Python has
  // stopped working" modal.
  //
  // Note: we deliberately do NOT pass ``creationFlags: CREATE_NO_WINDOW``
  // (0x08000000) to spawn.  Combined with ``windowsHide: true`` that flag
  // is redundant, and on some Windows builds it can cause the child's
  // stdio pipes to be closed prematurely — the agent loop then appears
  // to "lose" history messages because the server log lines that the
  // session/index depends on never reach the renderer.
  cleanEnv["PYTHONFAULTHANDLER"] = "1";
  cleanEnv["PYTHONUNBUFFERED"] = "1";

  serverProcess = spawn(pythonCmd, args, {
    cwd,
    stdio: ["ignore", "pipe", "pipe"],
    env: cleanEnv,
    windowsHide: true,
  });

  serverProcess.stdout?.on("data", (data: Buffer) => {
    console.log(`[pepsicode-server] ${data.toString().trim()}`);
  });

  serverProcess.stderr?.on("data", (data: Buffer) => {
    console.error(`[pepsicode-server] ${data.toString().trim()}`);
  });

  serverProcess.on("exit", (code) => {
    console.log(`Python server exited with code ${code}`);
  });

  // Wait for the server to be ready by polling the port
  const ready = await waitForPort(port, 10000);
  if (!ready) {
    console.error(`[electron] Python server did not become ready on port ${port}`);
  }
}

function waitForPort(port: number, timeoutMs: number): Promise<boolean> {
  const start = Date.now();
  return new Promise((resolve) => {
    const check = () => {
      const socket = new net.Socket();
      socket.setTimeout(500);
      socket.on("connect", () => {
        socket.destroy();
        resolve(true);
      });
      socket.on("error", () => {
        socket.destroy();
        if (Date.now() - start > timeoutMs) {
          resolve(false);
        } else {
          setTimeout(check, 300);
        }
      });
      socket.on("timeout", () => {
        socket.destroy();
        if (Date.now() - start > timeoutMs) {
          resolve(false);
        } else {
          setTimeout(check, 300);
        }
      });
      socket.connect(port, "127.0.0.1");
    };
    check();
  });
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 600,
    title: "Pepsicode",
    backgroundColor: "#ffffff",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
  });

  const isDev = !app.isPackaged;
  if (isDev && process.env.PEPSI_DEV_SERVER === "1") {
    // Hot-reload dev mode: requires `npm run dev` running separately
    mainWindow.loadURL("http://localhost:5173");
    mainWindow.webContents.openDevTools({ mode: "detach" });
  } else {
    // Default: load built static files (works for both dev and production)
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function killStaleServers(): void {
  // Best-effort: kill any leftover pepsicode python server processes from
  // previous runs so they don't hold onto ports and confuse the client.
  try {
    const { execSync } = require("child_process");
    // Find PIDs listening on 127.0.0.1 in the 8765..8785 range that are
    // python processes running pepsicode.server.
    let out = "";
    try {
      out = execSync('netstat -ano | findstr "127.0.0.1:876"', { encoding: "utf8" });
    } catch {
      // findstr returns non-zero when no match; ignore.
    }
    const seen = new Set<number>();
    for (const line of out.split(/\r?\n/)) {
      const m = line.match(/127\.0\.0\.1:(87\d{2})\s+\S+\s+\S+\s+(\d+)/);
      if (!m) continue;
      const pid = Number(m[2]);
      if (pid && !seen.has(pid)) {
        seen.add(pid);
        try {
          // Only kill python processes to avoid touching unrelated services.
          const tasklist = execSync(`tasklist /FI "PID eq ${pid}" /NH /FO CSV`, {
            encoding: "utf8",
          });
          if (/python|pepsicode/i.test(tasklist)) {
            execSync(`taskkill /F /PID ${pid}`, { stdio: "ignore" });
            console.log(`[electron] Killed stale server PID ${pid}`);
          }
        } catch {
          // ignore individual failures
        }
      }
    }
  } catch (e) {
    console.warn("[electron] killStaleServers failed:", e);
  }
}

app.whenReady().then(async () => {
  try {
    killStaleServers();
    serverPort = await findFreePort(8765);
    console.log(`[electron] Using port ${serverPort}`);
    await startPythonServer(serverPort);
  } catch (err) {
    console.error("Failed to start Python server:", err);
  }

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
});

ipcMain.handle("get-server-port", () => serverPort);

ipcMain.handle("select-folder", async () => {
  if (!mainWindow) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"],
    title: "选择项目文件夹",
  });
  return result.canceled ? null : result.filePaths[0];
});
