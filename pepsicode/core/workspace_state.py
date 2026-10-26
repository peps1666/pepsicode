"""Host-owned content fingerprints for verification, never model-supplied ignores."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

EXCLUDED = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".eval-work",
        ".venv",
        "venv",
        "build",
        "dist",
        "release",
        ".next",
        "coverage",
    }
)
RUNTIME = frozenset({"executions", "tool-results", "cache", "context", "contexts"})


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def task_paths(task):
    return [a["path"] for a in task.get("required_artifacts", [])] + [
        c["path"] for c in task.get("criteria", []) if "path" in c
    ]


def _content_version(path, remaining, scope=None):
    before = path.stat()
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if scope:
                scope.check_cancelled()
            size += len(block)
            if size > remaining:
                raise ValueError("Verification snapshot exceeds 512 MiB; use a smaller workspace")
            hasher.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f"Verification input changed while hashing: {path}")
    return hasher.hexdigest(), size


def workspace_state(cwd, *, include_paths=()):
    root = Path(cwd).resolve()
    if not root.is_dir():
        raise ValueError("Verification workspace is not a directory")
    started = time.monotonic()
    entries = {}
    size = 0
    from pepsicode.core import session as sessions
    from pepsicode.execution.runtime import current_scope

    owned = {
        p
        for p in (Path(sessions.PEPSI_CODE_DIR).resolve(), Path(sessions.SESSIONS_DIR).resolve())
        if p.is_relative_to(root)
    }
    scope = current_scope()
    journal = scope.journal.path.resolve() if scope and scope.journal else None

    def unreadable(error):
        raise error

    for directory, names, files in os.walk(root, followlinks=False, onerror=unreadable):
        if scope:
            scope.check_cancelled()
        relative = Path(directory).relative_to(root)
        names[:] = sorted(
            name
            for name in names
            if name not in EXCLUDED
            and not name.startswith((".pytest_", "pip-"))
            and not name.endswith(".egg-info")
            and not (relative == Path(".pepsi-code") and name in RUNTIME)
            and (not owned or (Path(directory) / name).resolve() not in owned)
        )
        for name in sorted([*files, *(name for name in names if (Path(directory) / name).is_symlink())]):
            if scope:
                scope.check_cancelled()
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(directory) / name
            if journal is not None and path.name == journal.name and path.resolve() == journal:
                continue
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                # External content is outside this local proof; do not follow it.
                if not path.resolve().is_relative_to(root):
                    raise ValueError(f"Verification input symlink leaves workspace: {key}")
                entries[key] = "link:" + os.readlink(path)
                if path.is_dir():
                    continue
            hashed, read = _content_version(path, 512 * 1024 * 1024 - size, scope)
            size += read
            entries[key] = entries.get(key, "") + hashed
            if len(entries) > 100_000:
                raise ValueError("Verification snapshot exceeds 100000 files; use a smaller workspace")
    # Explicit artifacts stay covered even in generated output directories.
    for value in include_paths:
        path = root / value
        if not path.resolve().is_relative_to(root):
            raise ValueError("Explicit verification inputs must stay in the workspace")
        key = path.relative_to(root).as_posix()
        if key not in entries:
            if path.exists():
                entries[key], read = _content_version(path, 512 * 1024 * 1024 - size, scope)
                size += read
            else:
                entries[key] = "missing"
    if len(entries) > 100_000:
        raise ValueError("Verification snapshot exceeds 100000 files; use a smaller workspace")
    return {
        "version": 1,
        "digest": digest(entries),
        "files": len(entries),
        "bytes": size,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "entries": entries,
    }


def summary(state):
    return {key: value for key, value in state.items() if key != "entries"}
