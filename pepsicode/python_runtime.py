"""Choose a project's Python without treating the frozen server as an interpreter."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def project_python(cwd: str) -> str:
    if not getattr(sys, "frozen", False):
        return sys.executable
    relative = Path("Scripts/python.exe") if sys.platform == "win32" else Path("bin/python")
    for name in (".venv", "venv"):
        candidate = Path(cwd) / name / relative
        if candidate.is_file():
            return str(candidate)
    # A missing project interpreter produces an ordinary "not found" result.
    # It must never re-enter the frozen server with `-m pytest` or a hook script.
    return shutil.which("python") or shutil.which("python3") or "python"
