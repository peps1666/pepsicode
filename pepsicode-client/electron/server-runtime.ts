import { existsSync } from "node:fs";
import { join } from "node:path";

// Preserve user-defined provider variables while excluding Electron runtime
// settings that must not affect Python or its Node/MCP descendants.
export function pythonEnvironment(source: NodeJS.ProcessEnv, packaged = false): NodeJS.ProcessEnv {
  const env = Object.fromEntries(Object.entries(source).filter(([key]) =>
    !/^(ELECTRON_|CHROME_|CHROMIUM_|CRASHPAD_|NODE_OPTIONS$|NODE_INSPECT_RESUME_ON_START$)/i.test(key) &&
    !(packaged && /^(PYTHONHOME|PYTHONPATH|PYTHONSTARTUP)$/i.test(key))
  ));
  if (source.PEPSI_CODE_DESKTOP_DATA_DIR) {
    const home = join(source.PEPSI_CODE_DESKTOP_DATA_DIR, "backend-home");
    env.USERPROFILE = home;
    env.HOME = home;
  }
  return env;
}

export interface BackendLaunch {
  command: string;
  args: string[];
  cwd: string;
  packaged?: boolean;
}

export function backendLaunch(options: {
  packaged: boolean; resourcesPath: string; projectRoot: string; workspace: string;
  env: NodeJS.ProcessEnv; exists?: (path: string) => boolean;
}): BackendLaunch {
  if (!options.packaged) return {
    command: options.env.PEPSI_PYTHON || "python",
    args: ["-m", "pepsicode.server"], cwd: options.projectRoot,
  };
  const command = join(options.resourcesPath, "backend", "pepsicode-server.exe");
  if (!(options.exists || existsSync)(command)) throw new Error("缺少内置后端。请完整解压 ZIP 后再启动 Pepsicode.exe。");
  return { command, args: [], cwd: options.workspace, packaged: true };
}

export function trustedRenderer(candidate: string, expected: string): boolean {
  try {
    const actual = new URL(candidate);
    const wanted = new URL(expected);
    return wanted.protocol === "file:"
      ? actual.protocol === "file:" && actual.host === wanted.host && actual.pathname === wanted.pathname
      : actual.origin === wanted.origin;
  } catch { return false; }
}

export function serverOrigins(renderer: string): string[] {
  const origin = new URL(renderer).origin;
  return origin === "null" ? ["null", "file://"] : ["null", "file://", origin];
}
