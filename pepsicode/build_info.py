"""Build identity is separate from a release version or a successful smoke."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pepsicode.version import VERSION


def source_identity(root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1]

    def git(*args):
        try:
            return subprocess.check_output(
                ["git", *args], cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=5
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return None

    toplevel = git("rev-parse", "--show-toplevel")
    # A wheel installed under some other checkout must never borrow that
    # project's commit identity. Editable source checkouts resolve to root.
    sha = git("rev-parse", "HEAD") if toplevel and Path(toplevel).resolve() == root.resolve() else None
    status = git("status", "--porcelain", "--untracked-files=normal") if sha else None
    tag = git("describe", "--exact-match", "--tags", "HEAD") if sha else None
    return {
        "schema_version": 1,
        "version": VERSION,
        "git_sha": sha,
        "dirty": bool(status) if status is not None else None,
        "channel": "tagged" if tag == f"v{VERSION}" and status == "" else "development",
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def build_identity():
    manifest = Path(__file__).with_name("build-info.json")
    if getattr(sys, "frozen", False):
        if not manifest.is_file():
            return {
                "version": VERSION,
                "git_sha": None,
                "dirty": None,
                "channel": "unknown",
                "identity_error": "Frozen build manifest missing",
            }
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if value.get("version") != VERSION or value.get("schema_version") != 1:
            raise ValueError("Bundled build identity does not match the runtime version")
        return value
    return source_identity()


def write_manifest(root, target):
    identity = {**source_identity(root), "built_at": datetime.now(UTC).isoformat()}
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return identity


def version_description():
    value = build_identity()
    sha = (value.get("git_sha") or "unknown")[:12]
    dirty = " dirty" if value.get("dirty") else ""
    return f"pepsicode {VERSION} ({value['channel']}, {sha}{dirty})"
