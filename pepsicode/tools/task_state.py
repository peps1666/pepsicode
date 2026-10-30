"""Declare reviewable acceptance conditions against an exact current task revision."""

from __future__ import annotations

import copy
import json

from pepsicode.core.task_acceptance import validate_criteria
from pepsicode.core.task_control import edit_control, is_continuation, snapshot
from pepsicode.tooling import ToolDefinition, ToolResult


def _validate(value):
    action = value.get("action", "inspect")
    if action not in {"inspect", "define", "revise", "classify", "correct_files"}:
        raise ValueError("action must be inspect, define, revise, classify, or correct_files")
    if action == "inspect":
        return {"action": action}
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("define requires the exact task revision")
    kind = value.get("kind")
    if kind is not None and kind not in {"answer", "change", "operation"}:
        raise ValueError("kind must be answer, change, or operation")
    if action in {"revise", "classify", "correct_files"} and (
        (action == "revise" and not isinstance(value.get("instruction_id"), str))
        or not isinstance(value.get("reason"), str)
        or not value["reason"].strip()
    ):
        raise ValueError("revise requires the latest human instruction_id and a reason")
    roles = value.get("file_roles", [])
    if not isinstance(roles, list) or len(roles) > 32:
        raise ValueError("file_roles must be an array of at most 32 corrections")
    from pepsicode.core.file_contract import ROLES

    for item in roles:
        if not isinstance(item, dict) or set(item) != {"path", "role"} or item["role"] not in ROLES:
            raise ValueError("file_roles needs path and a known role")
        validate_criteria([{"id": "role", "kind": "file_exists", "path": item["path"]}])
    if action == "correct_files" and not roles:
        raise ValueError("correct_files requires file_roles and a reason")
    return {
        "action": action,
        "task_id": value.get("task_id"),
        "revision": revision,
        "kind": kind,
        "criteria": validate_criteria(value.get("criteria", [])),
        "instruction_id": value.get("instruction_id"),
        "reason": value.get("reason"),
        "file_roles": roles,
    }


def _run(value, context):
    if value["action"] in {"define", "revise", "classify", "correct_files"}:
        with edit_control(context) as control:
            task = control.get("task")
            if not task or (value["task_id"], value["revision"]) != (task["id"], task["revision"]):
                raise PermissionError("Stale task identity; inspect task_state before defining acceptance")
            if task.get("admitted_revision") != task["revision"]:
                raise PermissionError(
                    "Wait for pending human instructions to be delivered before changing the contract"
                )
            if value["action"] == "correct_files":
                from pepsicode.core.file_contract import correct_file_roles

                correct_file_roles(
                    task, value["file_roles"], reason=value["reason"], instruction_id=value["instruction_id"]
                )
                return ToolResult(True, json.dumps(task, ensure_ascii=False))
            classification = task.get("classification", {"locked": True})
            if task["kind"] == "answer" and classification.get("project_overview") and value["criteria"]:
                raise PermissionError(
                    "A project overview does not require model-defined acceptance checks; answer from observed files"
                )
            if value["kind"] == "answer" and task["kind"] != "answer":
                if value["action"] != "classify" or classification.get("locked", True) or task["criteria"]:
                    raise PermissionError("The current actionable task cannot be downgraded to an answer")
            if value["kind"] not in {None, "answer"} and classification.get("read_only_request"):
                raise PermissionError("The user's explain-only restriction cannot be expanded by classification")
            previous_kind = task["kind"]
            if value["kind"]:
                task["kind"] = value["kind"]
                if previous_kind != value["kind"]:
                    if value["kind"] == "answer":
                        task["required_artifacts"] = [
                            a for a in task.get("required_artifacts", []) if a["source"] != "inferred"
                        ]
                    task.setdefault("classification_history", []).append(
                        {"before": classification, "reason": value.get("reason"), "revision": task["revision"]}
                    )
                    task["classification"] = {
                        **classification,
                        "source": "model",
                        "kind": value["kind"],
                        "reason": value.get("reason"),
                    }
                    task["delivered_intent"] = copy.deepcopy(task["classification"])
            existing = {item["id"]: item for item in task["criteria"]}
            if value["action"] == "revise":
                if (
                    not task["instructions"]
                    or value["instruction_id"] != task.get("last_delivered_input_id", task["inputs"][-1])
                    or task.get("criteria_revision") == task["revision"]
                ):
                    raise PermissionError(
                        "Only a new accepted human instruction authorizes replacing acceptance conditions"
                    )
                if is_continuation(task["instructions"][-1]):
                    raise PermissionError("A request to continue does not authorize discarding requirements")
                task.setdefault("acceptance_changes", []).append(
                    {
                        "instruction_id": value["instruction_id"],
                        "instruction": task["instructions"][-1],
                        "before": task["criteria"],
                        "before_sources": copy.deepcopy(task.get("criterion_sources", {})),
                        "reason": value["reason"],
                    }
                )
                existing = {}
            for criterion in value["criteria"]:
                already_defined = criterion["id"] in existing
                if criterion.get("input_id", task["inputs"][-1]) not in task["inputs"]:
                    raise PermissionError("Criterion input_id must refer to an accepted task input")
                if criterion["id"] in existing and existing[criterion["id"]] != criterion:
                    raise PermissionError("Existing acceptance conditions cannot be silently replaced")
                existing[criterion["id"]] = criterion
                if not already_defined:
                    task.setdefault("criterion_sources", {})[criterion["id"]] = {
                        "source": "model",
                        "input_id": criterion.get("input_id", task["inputs"][-1]),
                        "purpose": criterion.get("purpose", criterion["id"]),
                    }
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
    description="Inspect task and classification. classify can correct an uncertain rule inference with a reason using exact id/revision, never downgrade a locked explicit action or expand explain-only restrictions. define adds acceptance. Only a new accepted human instruction permits revise with instruction_id and reason. Never discard requirements on your own.",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "define", "revise", "classify", "correct_files"]},
            "file_roles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": ["input", "output", "delete_target", "protected", "reference"],
                        },
                    },
                    "required": ["path", "role"],
                },
            },
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
                        "kind": {
                            "type": "string",
                            "enum": ["file_exists", "file_contains", "file_absent", "file_unchanged", "command"],
                        },
                        "path": {"type": "string"},
                        "text": {"type": "string"},
                        "input_id": {"type": "string", "description": "Accepted user input this condition addresses"},
                        "purpose": {
                            "type": "string",
                            "description": "What this check can establish about the requested work",
                        },
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
