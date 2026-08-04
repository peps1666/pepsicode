from __future__ import annotations

import json
import logging
from pathlib import Path

from pepsicode.worktree.models import WorktreeSession

log = logging.getLogger(__name__)

SESSION_FILENAME = "worktree-session.json"


def _session_path(pepsi_dir: Path) -> Path:
    return pepsi_dir / SESSION_FILENAME


def save_worktree_session(pepsi_dir: Path, session: WorktreeSession | None) -> None:
    """Persist the current worktree session (or clear it when ``session`` is None)."""
    path = _session_path(pepsi_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if session is None:
        path.write_text("{}", encoding="utf-8")
        return
    data = {
        "original_cwd": session.original_cwd,
        "worktree_path": session.worktree_path,
        "worktree_name": session.worktree_name,
        "original_branch": session.original_branch,
        "original_head_commit": session.original_head_commit,
        "session_id": session.session_id,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_worktree_session(pepsi_dir: Path) -> WorktreeSession | None:
    """Load a persisted worktree session, or None if absent / invalid."""
    path = _session_path(pepsi_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data or "worktree_path" not in data:
            return None
        return WorktreeSession(
            original_cwd=data["original_cwd"],
            worktree_path=data["worktree_path"],
            worktree_name=data["worktree_name"],
            original_branch=data["original_branch"],
            original_head_commit=data["original_head_commit"],
            session_id=data.get("session_id", ""),
        )
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("Failed to load worktree session: %s", e)
        return None
