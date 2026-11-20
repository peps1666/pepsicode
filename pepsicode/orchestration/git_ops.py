"""Host-controlled Git entry for managed orchestration work (M2/M4).

Every git invocation from managed preparation, integration and delivery goes
through :func:`run_git`: a sanitized environment (no user config, no hooks,
no terminal prompt), journaled through the execution layer so cancellations,
deadlines and unknown outcomes are handled like any other command. Mutation
commands that touch the shared ``.git`` metadata must be serialized by the
caller with :func:`pepsicode.orchestration.runtime.own_workspace`.
"""

from __future__ import annotations

import os
from pathlib import Path

from pepsicode.execution import process
from pepsicode.tooling import ToolContext

_GIT_ENV_KEYS = ("PATH", "SystemRoot", "WINDIR", "SystemDrive", "PATHEXT")


def run_git(context: ToolContext, cwd, *arguments, allowed=(0,), timeout=60):
    """Run one git command under the sanitized managed environment."""
    env = {name: os.environ[name] for name in _GIT_ENV_KEYS if name in os.environ}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    result = process.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", *arguments],
        context=context,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if result.returncode not in allowed:
        raise RuntimeError(f"Git {arguments[0]} failed ({result.returncode}): {result.stderr.strip()[:800]}")
    return result


class WorktreeBaselineError(ValueError):
    """The opened workspace cannot supply a baseline for isolated Workers."""


def has_worktree_baseline(context: ToolContext, workspace: str) -> bool:
    """Return True when ``workspace`` is a Git repository root with a committed HEAD."""
    repo = Path(workspace).resolve()
    try:
        result = run_git(context, repo, "rev-parse", "--show-toplevel", "--verify", "HEAD^{commit}", allowed=(0, 128))
    except (FileNotFoundError, OSError):
        return False
    lines = result.stdout.splitlines()
    return result.returncode == 0 and bool(lines) and Path(lines[0]).resolve() == repo


def check_worktree_baseline(context: ToolContext, workspace: str) -> None:
    repo = Path(workspace).resolve()
    result = run_git(context, repo, "rev-parse", "--show-toplevel", "--verify", "HEAD^{commit}", allowed=(0, 128))
    lines = result.stdout.splitlines()
    if lines:
        root = Path(lines[0]).resolve()
        if root != repo:
            hint = "该仓库还需要一个有效的 HEAD 提交。" if result.returncode else ""
            raise WorktreeBaselineError(
                f"当前会话打开的是子目录 {repo}。请从 Git 仓库根目录 {root} 启动新的 PepsiCode 会话，"
                f"再发送原来的 Worker 任务。{hint}本次派工未入队。"
            )
        if result.returncode:
            raise WorktreeBaselineError(
                f"当前会话已在 Git 仓库根目录 {root}，但没有可用的 HEAD 提交。"
                "请先创建首次提交，再发送 Worker 任务。本次派工未入队。"
            )
    if result.returncode:
        raise WorktreeBaselineError(
            f"独立实施 Worker 需要在有提交的 Git 仓库根目录启动。当前目录 {repo} 无可用基线，"
            f"本次派工未入队。Git: {result.stderr.strip()[:800]}"
        )
