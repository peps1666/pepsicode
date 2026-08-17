import { app, BrowserWindow, ipcMain, dialog } from "electron";
import { spawn, ChildProcess } from "child_process";
import * as path from "path";
import * as net from "net";

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

  serverProcess = spawn(pythonCmd, args, {
    cwd,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env },
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
