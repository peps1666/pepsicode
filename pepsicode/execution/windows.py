"""Resolve Windows command shims before approval, without implicit shell parsing."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def _which(command: str, cwd: Path, environment: dict[str, str]) -> Path | None:
    extensions = environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
    names = [command] if Path(command).suffix else [command + ext for ext in extensions if ext]
    if Path(command).parent != Path(".") or command.startswith("."):
        roots = [cwd]
    else:
        # Unlike shutil.which on older Python, never search the host's cwd.
        roots = [cwd, *(cwd / entry.strip('"') for entry in environment.get("PATH", os.defpath).split(os.pathsep))]
    return next((root / name for root in roots for name in names if (root / name).is_file()), None)


def resolve_argv(argv: list[str], cwd: Path, environment: dict[str, str]) -> list[str]:
    if os.name != "nt":
        return argv
    resolved = _which(argv[0], cwd, environment)
    if not resolved or resolved.suffix.lower() not in {".cmd", ".bat"}:
        return argv
    values = [str(Path(resolved).resolve()), *argv[1:]]
    # Batch files can re-expand arguments via %* or CALL. Do not silently turn
    # literal argv into shell code; explicit shell commands remain available.
    if any(re.search(r'["&|<>^%!\r\n]', value) for value in values):
        raise ValueError("Windows batch arguments contain shell control characters; use an explicit shell command")
    shell = _which("cmd.exe", cwd, environment)
    if not shell:
        raise FileNotFoundError("cmd.exe was not found on PATH")
    payload = '"' + " ".join(f'"{value}"' for value in values) + '"'
    return [str(shell.resolve()), "/d", "/v:off", "/s", "/c", payload]


def popen_args(argv: list[str]) -> list[str] | str:
    # list2cmdline escapes inner quotes for the C runtime. cmd.exe /s /c uses
    # its own quoting rules; pass this already-quoted payload verbatim.
    if (
        os.name == "nt"
        and len(argv) == 6
        and Path(argv[0]).name.lower() == "cmd.exe"
        and argv[1:5] == ["/d", "/v:off", "/s", "/c"]
    ):
        return subprocess.list2cmdline(argv[:5]) + " " + argv[5]
    return argv
