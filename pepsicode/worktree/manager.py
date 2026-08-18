from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from pepsicode.worktree.changes import count_worktree_changes
from pepsicode.worktree.models import Worktree, WorktreeSession
from pepsicode.worktree.session import load_worktree_session, save_worktree_session
from pepsicode.worktree.setup import perform_post_creation_setup
from pepsicode.worktree.slug import flatten_slug, validate_slug

log = logging.getLogger(__name__)

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


class WorktreeError(Exception):
    """Raised on worktree operation failures (invalid name, git errors, dirty removal, ...)."""


class WorktreeManager:
    """Manage isolated git worktrees under ``<repo>/.pepsi-code/worktrees/``.

    The manager is intentionally stateless across instantiations: the only
    persistent state is the on-disk worktree directory plus the optional
    session file.  This means callers can construct a fresh manager per
    request without losing context.
    """

    def __init__(
        self,
        repo_root: str,
        symlink_directories: list[str] | None = None,
        worktree_dir: str | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.symlink_directories = symlink_directories or []
        # worktree 物理目录默认在 <repo>/.pepsi-code/worktrees/
        # 这样不会污染主仓库的 .git/worktrees 元数据视图
        self.worktree_dir = worktree_dir or str(Path(repo_root) / ".pepsi-code" / "worktrees")
        self._pepsi_dir = Path(repo_root) / ".pepsi-code"
        self.active: dict[str, Worktree] = {}
        self.current_session: WorktreeSession | None = None

    # ------------------------------------------------------------------
    # 底层 git 调用
    # ------------------------------------------------------------------

    def _run_git(self, args: list[str], cwd: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        from pepsicode.subprocess_utils import hide_window_kwargs

        env = {**os.environ, **GIT_ENV}
        return subprocess.run(
            ["git"] + args,
            cwd=cwd or self.repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            **hide_window_kwargs(),
            stdin=subprocess.DEVNULL,
            env=env,
        )

    # ------------------------------------------------------------------
    # 快速恢复:直接从文件系统读取 HEAD SHA,无需启动 git 子进程
    # ------------------------------------------------------------------

    @staticmethod
    def read_worktree_head_sha(wt_path: str) -> str | None:
        """Read HEAD commit SHA of a worktree by parsing .git files directly.

        Avoids spawning a ``git`` subprocess - useful for cheap pre-flight
        checks (e.g. during stale cleanup).  Returns None if the worktree
        directory or its git metadata is missing/corrupt.
        """
        wt = Path(wt_path)
        git_file = wt / ".git"
        if not git_file.exists():
            return None

        try:
            content = git_file.read_text(encoding="utf-8").strip()
            if not content.startswith("gitdir:"):
                return None
            gitdir = Path(content.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (wt / gitdir).resolve()

            commondir_file = gitdir / "commondir"
            if commondir_file.exists():
                commondir_rel = commondir_file.read_text(encoding="utf-8").strip()
                commondir = (gitdir / commondir_rel).resolve()
            else:
                commondir = gitdir

            head_file = gitdir / "HEAD"
            if not head_file.exists():
                return None
            head_content = head_file.read_text(encoding="utf-8").strip()

            if head_content.startswith("ref:"):
                ref_path = head_content.split(":", 1)[1].strip()
                ref_file = gitdir / ref_path
                if not ref_file.exists():
                    ref_file = commondir / ref_path
                if ref_file.exists():
                    return ref_file.read_text(encoding="utf-8").strip()
                # ref 不存在则可能在 packed-refs 里
                packed_refs = commondir / "packed-refs"
                if packed_refs.exists():
                    for line in packed_refs.read_text(encoding="utf-8").splitlines():
                        if line.strip() and not line.startswith("#"):
                            parts = line.split()
                            if len(parts) == 2 and parts[1] == ref_path:
                                return parts[0]
                return None
            # detached HEAD:直接是 SHA
            return head_content
        except OSError:
            return None

    # ------------------------------------------------------------------
    # 创建 worktree
    # ------------------------------------------------------------------

    def create(self, name: str, base_branch: str = "HEAD") -> Worktree:
        """Create a new worktree named ``name`` based on ``base_branch``.

        If a worktree directory with the same slug already exists on disk
        and has a valid HEAD, we fast-recover it instead of erroring out
        (handles pepsicode restart mid-session).
        """
        err = validate_slug(name)
        if err:
            raise WorktreeError(err)

        if name in self.active:
            raise WorktreeError(f"worktree already exists: {name}")

        flat_slug = flatten_slug(name)
        wt_path = os.path.join(self.worktree_dir, flat_slug)
        branch_name = f"worktree-{flat_slug}"

        # 快速恢复:目录已存在且有有效 HEAD,直接复用
        head_sha = self.read_worktree_head_sha(wt_path)
        if head_sha is not None:
            log.info("Fast recovery: reusing existing worktree at %s", wt_path)
            wt = Worktree(
                name=name,
                path=wt_path,
                branch=branch_name,
                based_on=base_branch,
                head_commit=head_sha,
            )
            self.active[name] = wt
            return wt

        os.makedirs(self.worktree_dir, exist_ok=True)

        result = self._run_git(
            [
                "worktree",
                "add",
                "-B",
                branch_name,
                wt_path,
                base_branch,
            ]
        )
        if result.returncode != 0:
            raise WorktreeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")

        perform_post_creation_setup(
            self.repo_root,
            wt_path,
            symlink_directories=self.symlink_directories,
        )

        head_sha = self.read_worktree_head_sha(wt_path) or ""
        wt = Worktree(
            name=name,
            path=wt_path,
            branch=branch_name,
            based_on=base_branch,
            head_commit=head_sha,
            created=datetime.now(),
        )
        self.active[name] = wt
        return wt

    # ------------------------------------------------------------------
    # 进入 worktree(切 cwd 到 worktree 路径,记录原状态)
    # ------------------------------------------------------------------

    def enter(self, name: str) -> WorktreeSession:
        """Mark ``name`` as the active worktree and switch cwd into it.

        Records the user's original cwd / branch / HEAD so ``exit`` can
        restore them.  Persists the session so a pepsicode restart can
        detect an in-progress worktree session.
        """
        wt = self.active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree not found: {name}")

        original_cwd = os.getcwd()
        original_branch = self._get_current_branch()
        original_head = self._get_head_commit()

        session = WorktreeSession(
            original_cwd=original_cwd,
            worktree_path=wt.path,
            worktree_name=name,
            original_branch=original_branch,
            original_head_commit=original_head,
        )
        self.current_session = session
        save_worktree_session(self._pepsi_dir, session)
        return session

    # ------------------------------------------------------------------
    # 退出 worktree
    # ------------------------------------------------------------------

    def exit(
        self,
        name: str,
        action: str = "keep",
        discard_changes: bool = False,
    ) -> None:
        """Exit worktree ``name``.

        - ``action="keep"``:  just clear the session, leave the worktree on disk
        - ``action="remove"``: also delete the worktree; refuses if there are
          uncommitted changes / new commits unless ``discard_changes=True``
        """
        wt = self.active.get(name)
        if wt is None:
            raise WorktreeError(f"worktree not found: {name}")

        if action == "remove" and not discard_changes:
            changes = count_worktree_changes(wt.path, wt.head_commit)
            if changes.uncommitted > 0 or changes.new_commits > 0:
                raise WorktreeError(
                    f"worktree has changes ({changes.uncommitted} uncommitted, "
                    f"{changes.new_commits} new commits). "
                    "Set discard_changes=True to force removal."
                )

        self.current_session = None
        save_worktree_session(self._pepsi_dir, None)

        if action == "remove":
            self._remove_worktree(name, wt)

    # ------------------------------------------------------------------
    # 删除 worktree(内部)
    # ------------------------------------------------------------------

    def _remove_worktree(self, name: str, wt: Worktree) -> None:
        result = self._run_git(["worktree", "remove", "--force", wt.path])
        if result.returncode != 0:
            log.warning("git worktree remove failed: %s", result.stderr.strip())

        # 删除对应分支
        flat_slug = flatten_slug(name)
        branch_name = f"worktree-{flat_slug}"
        self._run_git(["branch", "-D", branch_name])

        self.active.pop(name, None)

    # ------------------------------------------------------------------
    # 列出 / 查询
    # ------------------------------------------------------------------

    def list_worktrees(self) -> list[Worktree]:
        """Return all known worktrees.

        Merges the in-memory ``active`` dict with a filesystem scan of
        ``worktree_dir``.  This keeps the manager usable across
        stateless instantiations (e.g. one WorktreeManager per tool
        call): worktrees created by a previous instance are rediscovered
        from disk.
        """
        # 先把磁盘上存在但内存中没有的 worktree 补进来
        if os.path.isdir(self.worktree_dir):
            for entry in os.scandir(self.worktree_dir):
                if not entry.is_dir():
                    continue
                slug = entry.name
                # 反查 name:内存里有的就用内存的 name,否则用 slug 作为 name
                if any(flatten_slug(wt.name) == slug for wt in self.active.values()):
                    continue
                head_sha = self.read_worktree_head_sha(entry.path)
                if head_sha is None:
                    continue  # 不是有效 worktree 或已损坏
                wt = Worktree(
                    name=slug,
                    path=entry.path,
                    branch=f"worktree-{slug}",
                    based_on="unknown",
                    head_commit=head_sha,
                )
                self.active[slug] = wt
        return list(self.active.values())

    def get_current_session(self) -> WorktreeSession | None:
        return self.current_session

    # ------------------------------------------------------------------
    # 从持久化 session 中恢复(用于 pepsicode 重启后)
    # ------------------------------------------------------------------

    def restore_session(self) -> WorktreeSession | None:
        """Reload a worktree session from disk if one was persisted.

        Also re-registers the worktree in ``self.active`` so subsequent
        enter/exit calls find it.  Returns the restored session, or None.
        """
        session = load_worktree_session(self._pepsi_dir)
        if session is None:
            return None
        wt_path = session.worktree_path
        head_sha = self.read_worktree_head_sha(wt_path)
        if head_sha is None:
            # worktree 目录已不存在,清理 session 文件
            save_worktree_session(self._pepsi_dir, None)
            return None

        wt = Worktree(
            name=session.worktree_name,
            path=wt_path,
            branch=f"worktree-{flatten_slug(session.worktree_name)}",
            based_on="unknown",
            head_commit=head_sha,
        )
        self.active[session.worktree_name] = wt
        self.current_session = session
        return session

    # ------------------------------------------------------------------
    # 状态摘要(供 /worktree 命令和工具输出使用)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a serializable status snapshot."""
        return {
            "repo_root": self.repo_root,
            "worktree_dir": self.worktree_dir,
            "active_worktrees": [
                {
                    "name": wt.name,
                    "path": wt.path,
                    "branch": wt.branch,
                    "based_on": wt.based_on,
                    "head_commit": wt.head_commit[:12] if wt.head_commit else "",
                    "created": wt.created.isoformat(timespec="seconds"),
                }
                for wt in self.active.values()
            ],
            "current_session": {
                "worktree_name": self.current_session.worktree_name,
                "worktree_path": self.current_session.worktree_path,
                "original_cwd": self.current_session.original_cwd,
                "original_branch": self.current_session.original_branch,
            }
            if self.current_session
            else None,
        }

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_current_branch(self) -> str:
        try:
            result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            return result.stdout.strip() if result.returncode == 0 else "HEAD"
        except (subprocess.SubprocessError, OSError):
            return "HEAD"

    def _get_head_commit(self) -> str:
        try:
            result = self._run_git(["rev-parse", "HEAD"])
            return result.stdout.strip() if result.returncode == 0 else ""
        except (subprocess.SubprocessError, OSError):
            return ""
