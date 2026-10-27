import { app, BrowserWindow, ipcMain, dialog } from "electron";
import type { ChildProcess } from "node:child_process";
import { mkdirSync, createWriteStream, type WriteStream } from "node:fs";
import * as path from "node:path";
import * as net from "node:net";
import { randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";
import { backendLaunch, serverOrigins, trustedRenderer } from "./server-runtime";
import { launchBackend, stopBackend } from "./backend-process";
import { buildInformation, describeBuild } from "./build-info";

if (process.platform === "win32") {
  app.setAppUserModelId("com.pepsicode.client");
  // Preserve the Windows launch mode used by the existing desktop client.
  app.commandLine.appendSwitch("no-sandbox");
}
if (process.env.PEPSI_CODE_DESKTOP_DATA_DIR) {
  const dataDir = path.resolve(process.env.PEPSI_CODE_DESKTOP_DATA_DIR);
  mkdirSync(dataDir, { recursive: true });
  mkdirSync(path.join(dataDir, "backend-home"), { recursive: true });
  app.setPath("userData", dataDir);
}

let mainWindow: BrowserWindow | null = null;
let serverProcess: ChildProcess | null = null;
let serverPort = 0;
let quitting = false;
let backendLog: WriteStream | null = null;
const serverToken = randomBytes(32).toString("hex");

function rendererURL(): string {
  return !app.isPackaged && process.env.PEPSI_DEV_SERVER === "1"
    ? "http://localhost:5173"
    : pathToFileURL(path.join(__dirname, "..", "dist", "index.html")).href;
}

async function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address() as net.AddressInfo;
      probe.close((error) => error ? reject(error) : resolve(address.port));
    });
  });
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1400, height: 900, minWidth: 800, minHeight: 600,
    title: "Pepsicode", backgroundColor: "#ffffff",
    icon: path.join(__dirname, "..", "dist", "app-icon.png"),
    show: process.env.PEPSI_CODE_DESKTOP_SMOKE !== "1",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true, nodeIntegration: false,
    },
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
  });
  mainWindow.setMenuBarVisibility(false);
  void mainWindow.loadURL(rendererURL());
  if (!app.isPackaged && process.env.PEPSI_DEV_SERVER === "1") {
    mainWindow.webContents.openDevTools({ mode: "detach" });
  }
  mainWindow.on("closed", () => { mainWindow = null; });
}

async function start(): Promise<void> {
  const logPath = path.join(app.getPath("userData"), "backend.log");
  mkdirSync(path.dirname(logPath), { recursive: true });
  backendLog = createWriteStream(logPath, { flags: "a" });
  backendLog.on("error", (error) => console.error("Unable to write backend log:", error.message));
  try {
    const projectRoot = path.resolve(__dirname, "..", "..");
    const identity = buildInformation({ packaged: app.isPackaged, resourcesPath: process.resourcesPath,
      projectRoot, version: app.getVersion() });
    backendLog.write(`Build: ${JSON.stringify(identity)}\n`);
    const workspace = process.env.PEPSI_CODE_WORKSPACE ||
      (app.isPackaged ? path.join(app.getPath("documents"), "Pepsicode Projects") : projectRoot);
    mkdirSync(workspace, { recursive: true });
    const launch = backendLaunch({ packaged: app.isPackaged, resourcesPath: process.resourcesPath,
      projectRoot, workspace, env: process.env });
    serverPort = await findFreePort();
    backendLog.write(`\n[${new Date().toISOString()}] Starting backend on port ${serverPort}\n`);
    serverProcess = await launchBackend(launch, {
      port: serverPort, token: serverToken, origins: serverOrigins(rendererURL()),
      onLog: (text) => backendLog?.write(text),
    });
    serverProcess.once("exit", (code) => {
      serverProcess = null;
      if (!quitting) {
        dialog.showErrorBox("Pepsicode 后端已停止", `后端意外退出（${code}）。请重新启动应用。\n日志：${logPath}`);
        app.quit();
      }
    });
    createWindow();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    backendLog.write(`Startup failed: ${message}\n`);
    dialog.showErrorBox("Pepsicode 启动失败", `${message}\n\n请确认已完整解压 ZIP。\n日志：${logPath}`);
    app.quit();
  }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => {
    if (mainWindow?.isMinimized()) mainWindow.restore();
    mainWindow?.focus();
  });
  void app.whenReady().then(start);
}

app.on("activate", () => {
  if (!mainWindow && serverProcess && !quitting) createWindow();
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
app.on("before-quit", (event) => {
  if (quitting) return;
  quitting = true;
  if (serverProcess) {
    event.preventDefault();
    const child = serverProcess;
    serverProcess = null;
    void stopBackend(child).catch((error) => backendLog?.write(`Shutdown: ${error.message}\n`))
      .finally(() => { backendLog?.end(); app.quit(); });
  } else backendLog?.end();
});

ipcMain.handle("get-server-port", () => serverPort);
ipcMain.handle("get-server-connection", (event) => {
  if (!mainWindow || !serverProcess || event.sender !== mainWindow.webContents ||
      event.senderFrame !== mainWindow.webContents.mainFrame ||
      !trustedRenderer(event.senderFrame.url, rendererURL())) {
    throw new Error("Untrusted connection credential request or backend unavailable");
  }
  return { port: serverPort, token: serverToken };
});
ipcMain.handle("select-folder", async () => {
  if (!mainWindow) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"], title: "选择项目文件夹",
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("show-about", async (event) => {
  if (!mainWindow || event.sender !== mainWindow.webContents || event.senderFrame !== mainWindow.webContents.mainFrame ||
      !trustedRenderer(event.senderFrame.url, rendererURL())) throw new Error("Untrusted about request");
  const identity = buildInformation({ packaged: app.isPackaged, resourcesPath: process.resourcesPath,
    projectRoot: path.resolve(__dirname, "..", ".."), version: app.getVersion() });
  await dialog.showMessageBox(mainWindow, { type: "info", title: "关于 Pepsicode", message: "Pepsicode", detail: describeBuild(identity) });
});
