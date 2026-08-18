from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    from pepsicode.subprocess_utils import hide_window_kwargs

    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        **hide_window_kwargs(),
        env=env,
    )


@dataclass
class Changes:
    """Change counts for a worktree relative to its base commit."""

    uncommitted: int = 0
    new_commits: int = 0


def count_worktree_changes(wt_path: str, head_commit: str) -> Changes:
    """Count uncommitted files and new commits since ``head_commit``.

    On git errors we conservatively report 1 in each bucket so callers
    treat the worktree as "dirty" and refuse destructive cleanup.
    """
    changes = Changes()
    try:
        status = _run_git(["status", "--porcelain"], cwd=wt_path)
        if status.returncode == 0:
            changes.uncommitted = len([line for line in status.stdout.splitlines() if line.strip()])
    except (subprocess.SubprocessError, OSError):
        changes.uncommitted = 1

    if not head_commit:
        return changes

    try:
        rev_list = _run_git(["rev-list", "--count", f"{head_commit}..HEAD"], cwd=wt_path)
        if rev_list.returncode == 0:
            changes.new_commits = int(rev_list.stdout.strip() or 0)
    except (subprocess.SubprocessError, OSError, ValueError):
        changes.new_commits = 1

    return changes


def has_worktree_changes(wt_path: str, head_commit: str) -> bool:
    c = count_worktree_changes(wt_path, head_commit)
    return c.uncommitted > 0 or c.new_commits > 0


@dataclass
class CleanupResult:
    kept: bool
    path: str = ""
    branch: str = ""


def has_unpushed_commits(wt_path: str) -> bool:
    """Return True if HEAD has commits not present on any remote.

    Used by cleanup to avoid deleting worktrees with unpushed work.
    If no remotes are configured at all, returns False: without a push
    target, the concept of "unpushed" is meaningless and we shouldn't
    block cleanup.
    """
    try:
        # 先检查是否配置了任何 remote;没有 remote 就直接返回 False
        remote_result = _run_git(["remote"], cwd=wt_path)
        if remote_result.returncode != 0 or not remote_result.stdout.strip():
            return False

        result = _run_git(
            ["rev-list", "--max-count=1", "HEAD", "--not", "--remotes"],
            cwd=wt_path,
        )
        return bool(result.stdout.strip()) if result.returncode == 0 else True
    except (subprocess.SubprocessError, OSError):
        return True
