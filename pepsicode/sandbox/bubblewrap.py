"""Linux namespaces for managed Worker processes and their commands."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pepsicode.sandbox.backend import SandboxBackend, SandboxUnavailableError
from pepsicode.sandbox.types import ConfinedArgs, SandboxPolicy


class BubblewrapBackend(SandboxBackend):
    def __init__(self):
        executable = shutil.which("bwrap") if sys.platform == "linux" else None
        if executable is None:
            raise SandboxUnavailableError("Hardened Workers require Bubblewrap on Linux")
        self.executable = executable

    def confine(self, argv: list[str], policy: SandboxPolicy) -> ConfinedArgs:
        workspace = str(Path(policy.workspace_root).resolve())
        mounts = []
        for directory in ("/usr/bin", "/usr/lib", "/usr/lib64", "/usr/libexec", "/bin", "/lib", "/lib64"):
            if Path(directory).is_symlink():
                mounts.extend(("--symlink", str(Path(directory).resolve()), directory))
            elif Path(directory).is_dir():
                mounts.extend(("--ro-bind", directory, directory))
        return ConfinedArgs(
            argv=[
                self.executable,
                "--unshare-user",
                "--disable-userns",
                "--unshare-pid",
                "--unshare-net",
                "--unshare-ipc",
                "--unshare-uts",
                "--new-session",
                "--die-with-parent",
                "--cap-drop",
                "ALL",
                "--clearenv",
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--size",
                str(64 * 1024 * 1024),
                "--tmpfs",
                "/tmp",
                *mounts,
                "--bind",
                workspace,
                workspace,
                "--chdir",
                policy.cwd or workspace,
                "--",
                *argv,
            ],
            enforcement="full",
        )
