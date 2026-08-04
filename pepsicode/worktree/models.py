from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Worktree:
    """A single git worktree entry tracked by the manager."""

    name: str
    path: str
    branch: str
    based_on: str
    head_commit: str
    created: datetime = field(default_factory=datetime.now)


@dataclass
class WorktreeSession:
    """Persists the user's "currently inside worktree X" state across restarts.

    Stored in ``<repo>/.pepsi-code/worktree-session.json`` so that resuming
    pepsicode in the same repo can detect an active worktree session and
    restore the working directory / branch context.
    """

    original_cwd: str
    worktree_path: str
    worktree_name: str
    original_branch: str
    original_head_commit: str
    session_id: str = ""
