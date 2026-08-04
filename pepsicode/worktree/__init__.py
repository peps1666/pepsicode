"""Git worktree isolation for pepsicode.

Provides isolated git worktrees so the agent can experiment in parallel
branches without disturbing the main working tree.  Fully zero-dependency
(stdlib only), cross-platform (Windows/macOS/Linux).

Public API:
    WorktreeManager        - create/enter/exit/list worktrees
    Worktree, WorktreeSession - data classes
    WorktreeError          - error type
"""

from __future__ import annotations

from pepsicode.worktree.manager import WorktreeError, WorktreeManager
from pepsicode.worktree.models import Worktree, WorktreeSession

__all__ = [
    "WorktreeManager",
    "Worktree",
    "WorktreeSession",
    "WorktreeError",
]
