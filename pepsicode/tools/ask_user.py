from __future__ import annotations

import uuid

from pepsicode.core.task_control import edit_control
from pepsicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    question = input_data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question is required")
    required = input_data.get("required", True)
    if not isinstance(required, bool):
        raise ValueError("required must be a boolean")
    return {"question": question.strip(), "required": required}


def _run(input_data: dict, _context) -> ToolResult:
    question_id = _context.tool_use_id or uuid.uuid4().hex
    with edit_control(_context) as control:
        task = control.get("task")
        control.setdefault("questions", []).append(
            {
                "id": question_id,
                "task_id": task["id"] if task else None,
                "revision": task["revision"] if task else None,
                "question": input_data["question"],
                "required": input_data.get("required", True),
                "status": "pending",
            }
        )
    return ToolResult(ok=True, output=input_data["question"], awaitUser=input_data.get("required", True))


ask_user_tool = ToolDefinition(
    name="ask_user",
    description="Pause the turn and ask the user a clarifying question.",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}, "required": {"type": "boolean"}},
        "required": ["question"],
    },
    validator=_validate,
    run=_run,
    affects_workspace=False,
)
