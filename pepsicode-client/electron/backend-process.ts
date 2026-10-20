import { spawn, execFile, type ChildProcess } from "node:child_process";
import { join } from "node:path";
import { pythonEnvironment, type BackendLaunch } from "./server-runtime";

export async function stopBackend(child: ChildProcess): Promise<void> {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("后端进程未能及时退出")), 5000);
    child.once("exit", () => { clearTimeout(timer); resolve(); });
    if (process.platform === "win32") {
      // This PID is the child we created, never a port scan or an image-name match.
      execFile(join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe"),
        ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true }, (error) => {
          if (error && child.exitCode === null && child.signalCode === null) child.kill();
        });
    } else child.kill("SIGTERM");
  });
}

export async function launchBackend(launch: BackendLaunch, options: {
  port: number; token: string; origins: string[]; timeoutMs?: number;
  env?: NodeJS.ProcessEnv; onLog?: (text: string) => void;
}): Promise<ChildProcess> {
  const env = pythonEnvironment(options.env || process.env, launch.packaged);
  env.PEPSI_CODE_SERVER_TOKEN = options.token;
  env.PEPSI_CODE_SERVER_ORIGINS = JSON.stringify(options.origins);
  env.PYTHONUNBUFFERED = "1";
  env.PYTHONFAULTHANDLER = "1";
  const child = spawn(launch.command, [...launch.args, "--port", String(options.port), "--log-level", "INFO"], {
    cwd: launch.cwd, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
  });
  const log = (data: Buffer) => options.onLog?.(data.toString("utf8").replaceAll(options.token, "[redacted]"));
  child.stdout?.on("data", log);
  child.stderr?.on("data", log);
  try {
    await new Promise<void>((resolve, reject) => {
      let output = "";
      const marker = `pepsicode server listening on ws://127.0.0.1:${options.port}`;
      const finish = (error?: Error) => {
        clearTimeout(timer);
        child.stdout?.off("data", ready);
        child.off("error", failed);
        child.off("exit", exited);
        if (error) reject(error); else resolve();
      };
      const ready = (data: Buffer) => {
        output = (output + data.toString("utf8")).slice(-8192);
        if (output.split(/\r?\n/).some((line) => line.trim() === marker)) finish();
      };
      const failed = (error: Error) => finish(error);
      const exited = (code: number | null, signal: NodeJS.Signals | null) => finish(new Error(`后端启动失败（退出状态：${code ?? signal}）`));
      const timer = setTimeout(() => finish(new Error("后端启动超时，请查看 backend.log")), options.timeoutMs || 30000);
      child.stdout?.on("data", ready);
      child.once("error", failed);
      child.once("exit", exited);
    });
    return child;
  } catch (error) {
    await stopBackend(child);
    throw error;
  }
}
