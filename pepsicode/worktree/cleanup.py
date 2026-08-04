from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from pepsicode.worktree.changes import has_unpushed_commits, has_worktree_changes
from pepsicode.worktree.manager import WorktreeManager

log = logging.getLogger(__name__)

# 匹配"临时性"worktree 名称(由 agent 自动创建的短任务),只有这些会被自动清理
EPHEMERAL_PATTERNS = [
    re.compile(r"^agent-a[0-9a-f]{7}$"),
    re.compile(r"^wf-[a-zA-Z0-9._-]{1,55}$"),
    re.compile(r"^exp-[0-9a-f]{8}$"),
]


def _is_ephemeral(name: str) -> bool:
    return any(p.match(name) for p in EPHEMERAL_PATTERNS)


def cleanup_stale_worktrees(manager: WorktreeManager, cutoff_hours: int = 24) -> int:
    """清理陈旧的临时 worktree。

    只清理满足以下全部条件的 worktree:
    1. 名称匹配 EPHEMERAL_PATTERNS(避免误删用户命名的工作区)
    2. 不是当前活动 session
    3. mtime 早于 cutoff_hours 之前
    4. 没有未提交改动
    5. 没有未推送的 commit

    返回清理数量。同步实现,不依赖 asyncio。
    """
    cutoff = datetime.now() - timedelta(hours=cutoff_hours)
    removed = 0
    worktree_dir = Path(manager.worktree_dir)

    if not worktree_dir.exists():
        return 0

    for entry in worktree_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name

        if not _is_ephemeral(name):
            continue

        if manager.current_session and manager.current_session.worktree_name == name:
            continue

        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime > cutoff:
                continue
        except OSError:
            continue

        head_sha = WorktreeManager.read_worktree_head_sha(str(entry))
        if head_sha is None:
            continue

        if has_worktree_changes(str(entry), head_sha):
            continue

        if has_unpushed_commits(str(entry)):
            continue

        try:
            # 直接调 git 删除,不依赖 manager.active 状态
            result = manager._run_git(["worktree", "remove", "--force", str(entry)])
            if result.returncode == 0:
                manager._run_git(["branch", "-D", f"worktree-{name}"])
                removed += 1
                log.info("Cleaned up stale worktree: %s", name)
            else:
                log.warning("Failed to remove stale worktree %s: %s", name, result.stderr.strip())
        except Exception as e:
            log.warning("Failed to clean up stale worktree %s: %s", name, e)

    return removed
