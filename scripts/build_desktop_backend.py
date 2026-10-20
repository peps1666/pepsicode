"""Build the Windows backend in an isolated venv, including runtime data files."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path


def main() -> None:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("The ZIP backend must be built on Windows x64.")
    root = Path(__file__).resolve().parents[1]
    python = root / "build/desktop-venv/Scripts/python.exe"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(python.parents[1])
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--timeout",
            "20",
            "--retries",
            "2",
            "-r",
            str(root / "packaging/requirements-windows.txt"),
        ],
        check=True,
        cwd=root,
    )
    # Do not let host Python paths or Electron flags contaminate analysis.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "ELECTRON_RUN_AS_NODE"}
    }
    subprocess.run(
        [
            str(python),
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--console",
            "--noupx",
            "--name",
            "pepsicode-server",
            "--paths",
            str(root),
            "--collect-data",
            "pepsicode",
            "--copy-metadata",
            "websockets",
            "--copy-metadata",
            "psycopg2-binary",
            "--distpath",
            str(root / "build/desktop-backend"),
            "--workpath",
            str(root / "build/pyinstaller"),
            "--specpath",
            str(root / "build/pyinstaller"),
            str(root / "packaging/server_entry.py"),
        ],
        check=True,
        cwd=root,
        env=env,
    )
    executable = root / "build/desktop-backend/pepsicode-server/pepsicode-server.exe"
    for name in ("LICENSE.txt", "LICENSE_PYTHON.txt", "LICENSE"):
        license_path = Path(sys.base_prefix) / name
        if license_path.is_file():
            shutil.copyfile(license_path, executable.parent / "LICENSE-python.txt")
            break
    else:
        raise RuntimeError("Python license file not found in the build interpreter installation")
    subprocess.run([str(executable), "--self-test"], check=True, cwd=root / "build", env=env)


if __name__ == "__main__":
    main()
