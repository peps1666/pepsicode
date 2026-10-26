from __future__ import annotations

import copy
import time
import uuid

from pepsicode.core.task_control import edit_control
from pepsicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    todos = input_data.get("todos")
    if not isinstance(todos, list):
        raise ValueError("todos must be a list")
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            raise ValueError(f"todo[{i}] must be an object")
        if "content" not in todo:
            raise ValueError(f"todo[{i}] must have a 'content' field")
        if not isinstance(todo["content"], str) or not todo["content"].strip():
            raise ValueError("todo content must contain text")
        status = todo.get("status", "pending")
        if not isinstance(todo.get("required", True), bool):
            raise ValueError("todo required must be boolean")
        if todo.get("required") is False and not isinstance(todo.get("reason", ""), str):
            raise ValueError("optional todo reason must be text")
        if status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"todo[{i}] status must be 'pending', 'in_progress', or 'completed'")
    if len({todo["content"].strip() for todo in todos}) != len(todos):
        raise ValueError("todo content must be unique")
    return {"todos": todos}


def _run(input_data: dict, context) -> ToolResult:
    todos = input_data["todos"]
    with edit_control(context) as control:
        task_id = (control.get("task") or {}).get("id")
        previous = {task["content"]: task for task in control.get("todos", [])}
        tasks = []
        for todo in todos:
            content = todo["content"].strip()
            task = copy.deepcopy(previous.get(content)) or {
                "id": uuid.uuid4().hex[:12],
                "content": content,
                "created_at": time.time(),
            }
            task["status"] = todo.get("status", "pending")
            task["required"] = todo.get("required", True)
            task["reason"] = todo.get("reason", "")
            task["task_id"] = task_id
            task["completed_at"] = (task.get("completed_at") or time.time()) if task["status"] == "completed" else None
            tasks.append(task)
        control["todos"] = tasks

    # Format output
    lines = ["Task list updated:", ""]

    for task in tasks:
        status_icon = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }.get(task["status"], "?")

        lines.append(f"{status_icon} [{task['id']}] {task['content']}")

    lines.append("")

    # Summary
    pending = sum(1 for t in tasks if t["status"] == "pending")
    in_progress = sum(1 for t in tasks if t["status"] == "in_progress")
    completed = sum(1 for t in tasks if t["status"] == "completed")
    total = len(tasks)

    lines.extend(
        [
            f"Total: {total} | Pending: {pending} | In Progress: {in_progress} | Completed: {completed}",
        ]
    )

    return ToolResult(ok=True, output="\n".join(lines))


todo_write_tool = ToolDefinition(
    name="todo_write",
    affects_workspace=False,
    description="Create or update a list of tasks. Use this to track progress on multi-step tasks. Each task has content (required) and status (pending/in_progress/completed). Pass the complete list each time to update.",
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Complete list of tasks to track. Each task must have 'content' (string) and optionally 'status' (pending/in_progress/completed).",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Task description"},
                        "required": {
                            "type": "boolean",
                            "description": "False only for optional model-suggested work, never explicit user requirements.",
                        },
                        "reason": {"type": "string", "description": "Reason optional work is deferred."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Task status",
                        },
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["todos"],
    },
    validator=_validate,
    run=_run,
)
