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
        ".next",
        "coverage",
    }
)
RUNTIME = frozenset({"executions", "tool-results", "cache", "context", "contexts"})
MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 100_000


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def task_paths(task):
    return (
        [a["path"] for a in task.get("required_artifacts", [])]
        + [c["path"] for c in task.get("criteria", []) if "path" in c]
        + task.get("verification_inputs", [])
    )


def _content_version(path, remaining, scope=None):
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > remaining:
            raise ValueError("Verification snapshot exceeds 512 MiB; select the relevant project/package as workspace")
        while block := stream.read(1024 * 1024):
            if scope:
                scope.check_cancelled()
            size += len(block)
            if size > remaining:
                raise ValueError(
                    "Verification snapshot exceeds 512 MiB; select the relevant project/package as workspace"
                )
            hasher.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f"Verification input changed while hashing: {path}")
    return hasher.hexdigest(), size


def workspace_state(cwd, *, include_paths=(), reason="verification"):
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

    def collect(path, *, explicit=False, ancestors=(), entry=None):
        nonlocal size
        if scope:
            scope.check_cancelled()
        key = path.relative_to(root).as_posix()
        is_link = entry.is_symlink() if entry is not None else path.is_symlink()
        is_directory = entry.is_dir() if entry is not None else path.is_dir()
        if key in entries and not is_directory:
            return  # Reuse only within this single manifest construction.
        link = ""
        if is_link:
            target = path.resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"Verification input symlink leaves workspace: {key}")
            link = "link:" + os.readlink(path) + ":"
        if is_directory:
            # Resolve only directory aliases; ordinary entries use scandir's
            # cached type information, never mtime as a content proof.
            identity = path.resolve() if link else path
            if identity in ancestors:
                raise ValueError(f"Verification input directory cycle: {key}")
            entries[key] = link + "directory"
            with os.scandir(path) as stream:
                children = sorted(stream, key=lambda item: item.name)
            for child in children:
                child_path = path / child.name
                directory = child.is_dir(follow_symlinks=False)
                if not explicit and (
                    (
                        directory
                        and (
                            child.name in EXCLUDED
                            or child.name.startswith((".pytest_", "pip-"))
                            or child.name.endswith(".egg-info")
                        )
                    )
                    or (directory and child_path in owned)
                    or (path == root / ".pepsi-code" and child.name in RUNTIME)
                    or child.name.endswith((".pyc", ".pyo"))
                    or (journal is not None and child_path == journal)
                ):
                    continue
                collect(child_path, explicit=explicit, ancestors=(*ancestors, identity), entry=child)
        elif entry is None and not path.exists() and not is_link:
            entries[key] = "missing"
        else:
            hashed, read = _content_version(path, MAX_BYTES - size, scope)
            size += read
            entries[key] = link + hashed
        if len(entries) > MAX_FILES:
            raise ValueError(
                "Verification snapshot exceeds 100000 entries; select the relevant project/package as workspace"
            )

    # Explicit inputs may only enlarge host coverage. Directories are recursive
    # and missing inputs remain in the manifest so creation/deletion is visible.
    forced = []
    for value in sorted(set(include_paths)):
        path = root / value
        if Path(value).is_absolute() or ".." in Path(value).parts or not path.resolve().is_relative_to(root):
            raise ValueError("Explicit verification inputs must stay in the workspace")
        key = path.relative_to(root).as_posix()
        forced.append(key)
    collect(root)
    # Avoid counting already hashed entries twice when widening an excluded tree.
    for value in forced:
        if value in entries and entries[value] not in {"directory", "missing"}:
            continue
        collect(root / value, explicit=True)
    policy = {
        "version": 2,
        "excluded_cache_directories": sorted(EXCLUDED),
        "host_runtime_directories": sorted(RUNTIME),
        "host_owned_paths": sorted(p.relative_to(root).as_posix() for p in owned),
        "journal_rule": "Active host journal is runtime data, not verification input",
        "explicit_inputs": forced,
        "limits": {"bytes": MAX_BYTES, "entries": MAX_FILES},
        "boundaries": "Workspace content only; excluded dependencies, external services and modify-restore races are not frozen",
    }
    duration = round((time.monotonic() - started) * 1000, 3)
    if scope:
        scope.snapshot_metrics.append({"reason": reason, "files": len(entries), "bytes": size, "duration_ms": duration})
    return {
        "version": 2,
        "digest": digest([entries, policy]),
        "files": len(entries),
        "bytes": size,
        "duration_ms": duration,
        "policy": policy,
        "policy_digest": digest(policy),
        "entries": entries,
    }


def summary(state):
    return {key: value for key, value in state.items() if key != "entries"}
