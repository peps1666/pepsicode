from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# 这些本地配置文件不会被 git 跟踪,但新 worktree 需要它们才能正常工作
LOCAL_CONFIG_FILES = [
    "settings.local.json",
    ".env",
]


def perform_post_creation_setup(
    repo_root: str,
    wt_path: str,
    symlink_directories: list[str] | None = None,
) -> None:
    """Worktree 创建后立刻执行的初始化:
    1. 拷贝本地配置文件(.env 等)
    2. 复用主仓库的 git hooks 路径
    3. symlink 共享目录(node_modules / .venv 等)
    4. 按 .worktreeinclude 复制被 gitignore 的本地文件
    """
    root = Path(repo_root)
    wt = Path(wt_path)

    _copy_local_configs(root, wt)
    _setup_git_hooks(root, wt)
    _create_symlinks(root, wt, symlink_directories or [])
    _copy_ignored_files(root, wt)


def _copy_local_configs(root: Path, wt: Path) -> None:
    for name in LOCAL_CONFIG_FILES:
        src = root / name
        if src.exists():
            dst = wt / name
            try:
                shutil.copy2(str(src), str(dst))
                log.debug("Copied %s to worktree", name)
            except OSError as e:
                log.warning("Failed to copy %s: %s", name, e)


def _setup_git_hooks(root: Path, wt: Path) -> None:
    """让 worktree 复用主仓库的 git hooks(支持 husky 或原生 .git/hooks)。"""
    hooks_path: str | None = None

    husky_dir = root / ".husky"
    if husky_dir.is_dir():
        hooks_path = str(husky_dir)
    else:
        git_hooks = root / ".git" / "hooks"
        if git_hooks.is_dir():
            hooks_path = str(git_hooks)

    if hooks_path is None:
        return

    try:
        from pepsicode.subprocess_utils import hide_window_kwargs

        subprocess.run(
            ["git", "config", "core.hooksPath", hooks_path],
            cwd=str(wt),
            capture_output=True,
            timeout=10,
            **hide_window_kwargs(),
        )
        log.debug("Set core.hooksPath to %s in worktree", hooks_path)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("Failed to set hooks path: %s", e)


def _create_symlinks(root: Path, wt: Path, directories: list[str]) -> None:
    """Symlink 指定目录到 worktree,避免重新安装依赖。

    Windows 上创建符号链接需要开发者模式或管理员权限,失败时降级为
    警告并跳过(不会抛异常),worktree 仍可用,只是依赖目录为空。
    """
    for dirname in directories:
        src = root / dirname
        dst = wt / dirname
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            continue
        try:
            os.symlink(str(src), str(dst))
            log.debug("Symlinked %s to worktree", dirname)
        except OSError as e:
            # Windows 上常见: OSError [WinError 1314]: 客户端没有所需的特权
            log.warning("Failed to symlink %s (need admin/dev mode on Windows?): %s", dirname, e)


def _copy_ignored_files(root: Path, wt: Path) -> None:
    """根据主仓库的 .worktreeinclude 复制被 gitignore 的本地文件。

    .worktreeinclude 每行一个 fnmatch 模式,如 ``*.local.json`` 或 ``secrets/``。
    """
    include_file = root / ".worktreeinclude"
    if not include_file.exists():
        return

    try:
        patterns = [
            line.strip()
            for line in include_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return

    if not patterns:
        return

    try:
        from pepsicode.subprocess_utils import hide_window_kwargs

        result = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            **hide_window_kwargs(),
        )
        if result.returncode != 0:
            return
        ignored_files = [f.rstrip("/") for f in result.stdout.splitlines() if f.strip()]
    except (subprocess.SubprocessError, OSError):
        return

    for rel_path in ignored_files:
        if not any(fnmatch.fnmatch(rel_path, pat) for pat in patterns):
            continue
        src = root / rel_path
        dst = wt / rel_path
        if not src.is_file():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            log.debug("Copied ignored file %s to worktree", rel_path)
        except OSError as e:
            log.warning("Failed to copy ignored file %s: %s", rel_path, e)
