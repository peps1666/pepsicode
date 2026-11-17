"""Candidate records for delivered Worker outputs (M4 §20.2).

A candidate binds one delivered WorkItem to an exact code version: the managed
worktree branch, its base SHA and the committed head SHA plus a bounded diff
summary. Integration (§20.2) and delivery (§20.3) may only consume validated
candidates; a candidate whose worktree HEAD or branch no longer matches the
record is marked ``stale`` and cannot stand in for the recorded version (B17).
"""

from __future__ import annotations

import time
from pathlib import Path

from pepsicode.core.task_control import edit_control
from pepsicode.orchestration.git_ops import run_git
from pepsicode.tooling import ToolContext

_MAX_CHANGED_FILES = 32


def _task(session):
    task = session.task_control.get("task") or {}
    if task.get("status") != "active" or not isinstance(task.get("id"), str) or type(task.get("revision")) is not int:
        raise ValueError("No active parent task for candidate recording")
    return task


def _ready_attempt(session, work_id, task):
    state = session.task_control.get("orchestration") or {}
    lineage = [row for row in state.get("work", []) if row.get("work_id") == work_id]
    if not lineage:
        raise ValueError(f"No orchestration attempt for work item {work_id}")
    latest = lineage[-1]
    if (latest["task_id"], latest["revision"]) != (task["id"], task["revision"]):
        raise ValueError("Candidate belongs to another task revision")
    if latest.get("state") != "result_ready":
        raise ValueError("Only a delivered (result_ready) attempt can become a candidate")
    environments = state.get("environments", {})
    environment = environments.get(work_id)
    if (
        environment is None
        or environment.get("state") != "prepared"
        or environment.get("revision") != latest["revision"]
    ):
        raise ValueError("The attempt has no prepared managed environment")
    return latest, environment


def _head_info(context, checkout):
    branch = run_git(context, checkout, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    head = run_git(context, checkout, "rev-parse", "--verify", "HEAD").stdout.strip()
    dirty = run_git(context, checkout, "status", "--porcelain").stdout.strip()
    return branch, head, dirty


def _diff_summary(context, checkout, base_sha):
    stat = run_git(context, checkout, "diff", "--shortstat", f"{base_sha}..HEAD").stdout.strip()
    numstat = run_git(context, checkout, "diff", "--numstat", f"{base_sha}..HEAD").stdout.splitlines()
    files = [line.split("\t", 2)[-1].strip() for line in numstat if line.strip()]
    return {
        "shortstat": stat[:400],
        "changed_files": files[:_MAX_CHANGED_FILES],
        "changed_files_truncated": len(files) > _MAX_CHANGED_FILES,
    }


def record_candidate(session, scope, work_id):
    """Record one delivered attempt's committed output as a versioned candidate.

    Operates on the caller's session snapshot — the host reloads sessions at
    scheduling boundaries (see ``run_pending``), the same single-writer rule
    the M1 store and M2 preparation follow.
    """
    task = _task(session)
    latest, environment = _ready_attempt(session, work_id, task)
    repo = Path(session.workspace).resolve()
    if scope is None or scope.permissions is None:
        raise ValueError("Candidate recording requires an execution authority")
    checkout = Path(environment["worktree"])
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)
    branch, head, dirty = _head_info(context, checkout)
    if dirty:
        raise ValueError("The candidate worktree has uncommitted changes; the Worker must commit its deliverable")
    if head == environment["base_sha"]:
        raise ValueError("The candidate worktree has no commits beyond the base SHA")
    record = {
        "work_id": work_id,
        "attempt_id": latest["attempt_id"],
        "task_id": task["id"],
        "revision": task["revision"],
        "base_sha": environment["base_sha"],
        "branch": branch,
        "head_sha": head,
        "worktree": str(checkout),
        "state": "recorded",
        "recorded_at": time.time(),
        **_diff_summary(context, checkout, environment["base_sha"]),
    }
    with edit_control(session=session) as control:
        control.setdefault("orchestration", {}).setdefault("candidates", {})[work_id] = record
    return record


def validate_candidate(session, scope, work_id):
    """Revalidate a recorded candidate; a changed worktree HEAD marks it stale."""
    state = session.task_control.get("orchestration") or {}
    record = (state.get("candidates") or {}).get(work_id)
    if record is None:
        raise ValueError(f"No candidate record for work item {work_id}")
    if record.get("state") == "stale":
        return record
    context = ToolContext(str(session.workspace), permissions=scope.permissions, execution=scope)
    checkout = Path(record["worktree"])
    if not checkout.is_dir():
        env = (state.get("environments") or {}).get(work_id) or {}
        if env.get("state") == "pruned" and env.get("pruned_head") == record.get("head_sha"):
            return record
        stale = {**record, "state": "stale", "stale_reason": "worktree is missing"}
        with edit_control(session=session) as control:
            control.setdefault("orchestration", {}).setdefault("candidates", {})[work_id] = stale
        return stale
    branch, head, _dirty = _head_info(context, checkout)
    if (branch, head) != (record["branch"], record["head_sha"]):
        stale = {**record, "state": "stale", "stale_reason": "worktree head or branch changed"}
        with edit_control(session=session) as control:
            control.setdefault("orchestration", {}).setdefault("candidates", {})[work_id] = stale
        return stale
    return record
