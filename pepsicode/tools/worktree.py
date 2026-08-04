from __future__ import annotations

import json

from pepsicode.tooling import ToolDefinition, ToolResult
from pepsicode.worktree import WorktreeError, WorktreeManager


def _validate(input_data: dict) -> dict:
    action = input_data.get("action")
    if not isinstance(action, str) or not action:
        raise ValueError("action is required")
    if action not in ("create", "enter", "exit", "list", "status", "cleanup"):
        raise ValueError("action must be one of: create, enter, exit, list, status, cleanup")

    if action in ("create", "enter", "exit"):
        name = input_data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"name is required for action '{action}'")

    if action == "exit":
        action_on_exit = input_data.get("action_on_exit", "keep")
        if action_on_exit not in ("keep", "remove"):
            raise ValueError("action_on_exit must be 'keep' or 'remove'")

    return {
        "action": action,
        "name": input_data.get("name", ""),
        "base_branch": input_data.get("base_branch", "HEAD"),
        "action_on_exit": input_data.get("action_on_exit", "keep"),
        "force_remove": bool(input_data.get("force_remove", False)),
        "symlink_dirs": input_data.get("symlink_dirs", []),
        "cleanup_max_age_hours": float(input_data.get("cleanup_max_age_hours", 24)),
    }


def _run(input_data: dict, context) -> ToolResult:
    action = input_data["action"]
    cwd = context.cwd

    try:
        manager = WorktreeManager(
            repo_root=cwd,
            symlink_directories=input_data.get("symlink_dirs", []),
        )
        # 每次调用都先尝试恢复 session,保证多轮对话中状态连续
        manager.restore_session()
        # 同步磁盘上的 worktree 到内存 active 字典,这样 enter/exit/list
        # 即使针对非当前 session 的 worktree 也能找到
        manager.list_worktrees()

        if action == "create":
            return _do_create(manager, input_data)
        if action == "enter":
            return _do_enter(manager, input_data)
        if action == "exit":
            return _do_exit(manager, input_data)
        if action == "list":
            return _do_list(manager)
        if action == "status":
            return _do_status(manager)
        if action == "cleanup":
            return _do_cleanup(manager, input_data)
    except WorktreeError as e:
        return ToolResult(ok=False, output=f"Worktree error: {e}")
    except Exception as e:  # noqa: BLE001
        return ToolResult(ok=False, output=f"Unexpected error: {e}")

    return ToolResult(ok=False, output=f"Unknown action: {action}")


def _do_create(manager: WorktreeManager, input_data: dict) -> ToolResult:
    wt = manager.create(
        input_data["name"],
        base_branch=input_data.get("base_branch") or "HEAD",
    )
    return ToolResult(
        ok=True,
        output=(
            f"Created worktree '{wt.name}':\n"
            f"  path:   {wt.path}\n"
            f"  branch: {wt.branch}\n"
            f"  based:  {wt.based_on}\n"
            f"  head:   {wt.head_commit[:12]}\n"
            f"\nUse action='enter' with name='{wt.name}' to switch into it."
        ),
    )


def _do_enter(manager: WorktreeManager, input_data: dict) -> ToolResult:
    name = input_data["name"]
    session = manager.enter(name)
    # 注意:实际切换 cwd 需要调用者(agent)感知,这里只持久化 session 并提示
    return ToolResult(
        ok=True,
        output=(
            f"Entered worktree '{name}'.\n"
            f"  worktree path:  {session.worktree_path}\n"
            f"  original cwd:   {session.original_cwd}\n"
            f"  original branch: {session.original_branch}\n"
            f"\nSubsequent file/command operations should target: {session.worktree_path}\n"
            f"Use action='exit' with name='{name}' to leave."
        ),
    )


def _do_exit(manager: WorktreeManager, input_data: dict) -> ToolResult:
    name = input_data["name"]
    action_on_exit = input_data["action_on_exit"]
    manager.exit(name, action=action_on_exit, discard_changes=input_data["force_remove"])
    suffix = " (worktree removed from disk)" if action_on_exit == "remove" else " (worktree kept on disk)"
    return ToolResult(
        ok=True,
        output=f"Exited worktree '{name}'{suffix}.",
    )


def _do_list(manager: WorktreeManager) -> ToolResult:
    worktrees = manager.list_worktrees()
    if not worktrees:
        return ToolResult(ok=True, output="No active worktrees. Use action='create' to make one.")
    lines = ["Active worktrees:", ""]
    for wt in worktrees:
        marker = " *" if (manager.current_session and manager.current_session.worktree_name == wt.name) else "  "
        lines.append(
            f"{marker}{wt.name}\n"
            f"      path:   {wt.path}\n"
            f"      branch: {wt.branch}\n"
            f"      head:   {wt.head_commit[:12]}"
        )
    return ToolResult(ok=True, output="\n".join(lines))


def _do_status(manager: WorktreeManager) -> ToolResult:
    return ToolResult(ok=True, output=json.dumps(manager.status(), indent=2, ensure_ascii=False))


def _do_cleanup(manager: WorktreeManager, input_data: dict) -> ToolResult:
    from pepsicode.worktree.cleanup import cleanup_stale_worktrees

    cutoff = input_data["cleanup_max_age_hours"]
    removed = cleanup_stale_worktrees(manager, cutoff_hours=int(cutoff))
    return ToolResult(
        ok=True,
        output=f"Cleaned up {removed} stale worktree(s) older than {cutoff:.0f}h.",
    )


worktree_tool = ToolDefinition(
    name="worktree",
    description=(
        "Git worktree isolation tool. Allows working in isolated git worktrees for "
        "parallel experimentation without affecting the main working tree. "
        "Actions: create (new worktree on a branch), enter (switch into a worktree), "
        "exit (leave current worktree, optionally removing it), list (show active worktrees), "
        "status (JSON snapshot), cleanup (remove stale ephemeral worktrees). "
        "Use case: try risky changes in a worktree, then merge or discard without polluting main branch."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "enter", "exit", "list", "status", "cleanup"],
                "description": "Worktree action to perform.",
            },
            "name": {
                "type": "string",
                "description": "Worktree name (required for create/enter/exit). May contain '/' for grouping (e.g. 'feature/auth').",
            },
            "base_branch": {
                "type": "string",
                "description": "Base branch or commit for new worktree (default: HEAD).",
            },
            "action_on_exit": {
                "type": "string",
                "enum": ["keep", "remove"],
                "description": "On exit: 'keep' leaves worktree on disk; 'remove' deletes it (refuses if dirty unless force_remove=true). Default: keep.",
            },
            "force_remove": {
                "type": "boolean",
                "description": "Force remove worktree even with uncommitted changes (default: false).",
            },
            "symlink_dirs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Directories to symlink from main repo into new worktree (e.g. ['node_modules', '.venv']). Windows requires admin/dev mode.",
            },
            "cleanup_max_age_hours": {
                "type": "number",
                "description": "Cleanup: remove ephemeral worktrees older than N hours (default: 24).",
            },
        },
        "required": ["action"],
    },
    validator=_validate,
    run=_run,
)
