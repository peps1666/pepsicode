import { readFileSync } from "node:fs";
import { join } from "node:path";
import { execFileSync } from "node:child_process";

export interface BuildInfo {
  version: string; git_sha: string | null; dirty: boolean | null;
  channel: string; built_at?: string;
}

export function buildInformation(options: {
  packaged: boolean; resourcesPath: string; projectRoot: string; version: string;
}): BuildInfo {
  if (options.packaged) {
    const value = JSON.parse(readFileSync(join(options.resourcesPath, "build-info.json"), "utf8"));
    if (value.schema_version !== 1 || value.version !== options.version) {
      throw new Error("构建清单与桌面版本不一致，请重新完整解压匹配的发行包。");
    }
    return value;
  }
  const git = (args: string[]) => {
    try { return execFileSync("git", args, { cwd: options.projectRoot, encoding: "utf8", timeout: 5000, windowsHide: true, stdio: ["ignore", "pipe", "ignore"] }).trim(); }
    catch { return null; }
  };
  const git_sha = git(["rev-parse", "HEAD"]);
  const status = git_sha ? git(["status", "--porcelain", "--untracked-files=normal"]) : null;
  return { version: options.version, git_sha, dirty: status === null ? null : Boolean(status), channel: "development" };
}

export function describeBuild(value: BuildInfo): string {
  const dirty = value.dirty === null ? "工作区状态未知" : value.dirty ? "包含未提交修改" : "干净工作区";
  return `版本：${value.version}\n渠道：${value.channel}\n提交：${value.git_sha || "未知"}\n${dirty}\n构建时间：${value.built_at || "源码运行"}`;
}
