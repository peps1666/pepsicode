"""Declare reviewable acceptance conditions against an exact current task revision."""

from __future__ import annotations

import json

from pepsicode.core.task_acceptance import validate_criteria
from pepsicode.core.task_control import edit_control, snapshot
from pepsicode.tooling import ToolDefinition, ToolResult


def _validate(value):
    action = value.get("action", "inspect")
    if action not in {"inspect", "define", "revise"}:
        raise ValueError("action must be inspect, define, or revise")
    if action == "inspect":
        return {"action": action}
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("define requires the exact task revision")
    kind = value.get("kind")
    if kind is not None and kind not in {"answer", "change", "operation"}:
        raise ValueError("kind must be answer, change, or operation")
    if action == "revise" and (
        not isinstance(value.get("instruction_id"), str)
        or not isinstance(value.get("reason"), str)
        or not value["reason"].strip()
    ):
        raise ValueError("revise requires the latest human instruction_id and a reason")
    return {
        "action": action,
        "task_id": value.get("task_id"),
        "revision": revision,
        "kind": kind,
        "criteria": validate_criteria(value.get("criteria", [])),
        "instruction_id": value.get("instruction_id"),
        "reason": value.get("reason"),
    }


def _run(value, context):
    if value["action"] in {"define", "revise"}:
        with edit_control(context) as control:
            task = control.get("task")
            if not task or (value["task_id"], value["revision"]) != (task["id"], task["revision"]):
                raise PermissionError("Stale task identity; inspect task_state before defining acceptance")
            if value["kind"] == "answer" and task["kind"] != "answer":
                raise PermissionError("The current actionable task cannot be downgraded to an answer")
            previous_kind = task["kind"]
            if value["kind"]:
                task["kind"] = value["kind"]
            existing = {item["id"]: item for item in task["criteria"]}
            if value["action"] == "revise":
                if (
                    not task["instructions"]
                    or value["instruction_id"] != task["inputs"][-1]
                    or task.get("criteria_revision") == task["revision"]
                ):
                    raise PermissionError(
                        "Only a new accepted human instruction authorizes replacing acceptance conditions"
                    )
                if task["instructions"][-1].strip().casefold().rstrip(".!。！") in {
                    "继续",
                    "继续执行",
                    "接着做",
                    "continue",
                    "resume",
                }:
                    raise PermissionError("A request to continue does not authorize discarding requirements")
                task.setdefault("acceptance_changes", []).append(
                    {
                        "instruction_id": value["instruction_id"],
                        "instruction": task["instructions"][-1],
                        "before": task["criteria"],
                        "reason": value["reason"],
                    }
                )
                existing = {}
            for criterion in value["criteria"]:
                if criterion["id"] in existing and existing[criterion["id"]] != criterion:
                    raise PermissionError("Existing acceptance conditions cannot be silently replaced")
                existing[criterion["id"]] = criterion
            if previous_kind != task["kind"] or task["criteria"] != list(existing.values()):
                task["revision"] += 1
                task["admitted_revision"] = task["revision"]
            task["criteria"] = list(existing.values())
            task["criteria_revision"] = task["revision"]
            task["evidence"] = []
    state = snapshot(context)
    task = state.get("task")
    view = (
        {
            **task,
            "pending_questions": [
                q for q in state.get("questions", []) if q["task_id"] == task["id"] and q["status"] == "pending"
            ],
        }
        if task
        else None
    )
    return ToolResult(True, json.dumps(view, ensure_ascii=False))


task_state_tool = ToolDefinition(
    name="task_state",
    description="Inspect the task or define acceptance using exact id/revision. Definitions add conditions. Only a new accepted human instruction permits revise: provide its instruction_id, replacement criteria, and a reason. Never downgrade an actionable task or discard requirements on your own.",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "define", "revise"]},
            "instruction_id": {"type": "string"},
            "reason": {"type": "string"},
            "task_id": {"type": "string"},
            "revision": {"type": "integer"},
            "kind": {"type": "string", "enum": ["answer", "change", "operation"]},
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["file_exists", "file_contains", "command"]},
                        "path": {"type": "string"},
                        "text": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "kind"],
                },
            },
        },
    },
    validator=_validate,
    run=_run,
    affects_workspace=False,
    always_loaded=True,
)
