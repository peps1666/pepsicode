"""Create and CRC-verify the ZIP before replacing the distributable artifact."""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path


def create_archive(stage: Path, target: Path) -> None:
    for name in (
        "Pepsicode.exe",
        "PepsicodeDesktop.exe",
        "resources/app.asar",
        "resources/backend/pepsicode-server.exe",
    ):
        if not (stage / name).is_file():
            raise FileNotFoundError(f"Incomplete desktop staging: {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, filename = tempfile.mkstemp(prefix="pepsicode-archive-", suffix=".tmp", dir=target.parent)
    os.close(handle)
    temporary = Path(filename)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item in sorted(stage.rglob("*")):
                if item.is_symlink():
                    raise ValueError(f"Unexpected symbolic link in Windows staging: {item}")
                if item.is_file():
                    archive.write(item, item.relative_to(stage).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            invalid = archive.testzip()
            if invalid:
                raise ValueError(f"ZIP CRC check failed: {invalid}")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    client = Path(__file__).resolve().parents[1] / "pepsicode-client"
    version = json.loads((client / "package.json").read_text(encoding="utf-8"))["version"]
    target = client / "release" / f"Pepsicode-{version}-win-x64.zip"
    create_archive(client / "release/win-unpacked", target)
    print(f"CRC-verified ZIP: {target}")
