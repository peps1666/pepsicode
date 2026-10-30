"""User-controlled defaults for new sessions in individual workspaces."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pepsicode.config import PEPSI_CODE_DIR

WORKSPACE_PERMISSIONS_PATH = PEPSI_CODE_DIR / "workspace-permissions.json"
ACCESS_MODES = frozenset({"read_only", "auto", "trust_commands", "full_access"})


def _workspace_key(workspace: str) -> str:
    return os.path.normcase(str(Path(workspace).resolve()))


def _read() -> dict:
    try:
        data = json.loads(WORKSPACE_PERMISSIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid workspace permissions file")
    return data


def get_workspace_default(workspace: str) -> str:
    try:
        defaults = _read().get("workspaceDefaults", {})
        mode = defaults.get(_workspace_key(workspace)) if isinstance(defaults, dict) else None
    except (OSError, ValueError):
        return "auto"
    return mode if isinstance(mode, str) and mode in ACCESS_MODES else "auto"


def set_workspace_default(workspace: str, mode: str) -> None:
    if not isinstance(mode, str) or mode not in ACCESS_MODES:
        raise ValueError("Choose read_only, auto, trust_commands, or full_access")
    data = _read()
    defaults = data.get("workspaceDefaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("Invalid workspace defaults")
    data["workspaceDefaults"] = {**defaults, _workspace_key(workspace): mode}
    WORKSPACE_PERMISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=WORKSPACE_PERMISSIONS_PATH.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, WORKSPACE_PERMISSIONS_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
