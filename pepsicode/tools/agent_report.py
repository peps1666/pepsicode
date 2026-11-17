"""Report work for the execution that owns this tool instance."""

from __future__ import annotations

import uuid

from pepsicode.agents.trace import REPORT_LIMIT, REPORT_STATES, SUMMARY_LIMIT, AgentReport
from pepsicode.core.task_control import _fallback_lock, edit_control, owner
from pepsicode.execution.runtime import current_scope
from pepsicode.tooling import ToolCapability, ToolDefinition, ToolResult
from pepsicode.tools.ask_user import _validate as validate_question


def create_agent_report(manager, trace_id, parent_context):
    session, control = owner(parent_context)
    lock = session._tree_lock if session else _fallback_lock

    def validate(data):
        status, summary = data.get("status"), data.get("summary")
        if not isinstance(status, str) or status not in REPORT_STATES:
            raise ValueError("Invalid Agent report status")
        if not isinstance(summary, str) or not 0 < len(summary.strip()) <= SUMMARY_LIMIT:
            raise ValueError(f"summary must contain 1–{SUMMARY_LIMIT} characters")
        question = None
        if "question" in data:
            question = validate_question(data)
            if len(question["question"]) > SUMMARY_LIMIT:
                raise ValueError(f"question must contain at most {SUMMARY_LIMIT} characters")
        return status, summary.strip(), question

    def result(report):
        question = next((q for q in control.get("questions", []) if q["id"] == report.question_id), None)
        waiting = bool(question and question["required"] and question["status"] == "pending")
        return ToolResult(
            True,
            question["question"] if waiting else "Agent report saved.",
            awaitUser=waiting,
            details={"trace_id": trace_id, "report_id": report.id, "question_id": report.question_id},
        )

    def run(data, context):
        scope = current_scope()
        if scope.trace_id != trace_id:
            return ToolResult(False, "Agent report is bound to a different execution.", status="blocked")
        status, summary, question = data
        call_id = context.tool_use_id or uuid.uuid4().hex
        with lock:
            node = manager.get(trace_id)
            if node.ended_at is not None:
                return ToolResult(False, "Agent execution has ended.", status="blocked")
            previous = next((item for item in node.reports if item.id == call_id), None)
            if previous:
                return result(previous)
            if len(node.reports) >= REPORT_LIMIT:
                return ToolResult(False, "Agent report limit reached for this execution.", status="blocked")
            task = control.get("task") or {}
            stale = (task.get("id"), task.get("revision")) != (node.parent_task_id, node.spawn_revision)
            question_id = f"agent:{trace_id}:{call_id}" if question and not stale else None
            report = AgentReport(call_id, status, summary, node.spawn_revision, stale, question_id)
            manager.record_report(trace_id, report)
            try:
                with edit_control(parent_context, session=session) as parent_control:
                    if question_id:
                        parent_control.setdefault("questions", []).append(
                            {
                                "id": question_id,
                                "task_id": node.parent_task_id,
                                "revision": node.spawn_revision,
                                "trace_id": trace_id,
                                **question,
                                "status": "pending",
                            }
                        )
            except OSError as error:
                manager.rollback_report(trace_id, report)
                manager.set_persistence_error(str(error))
                return ToolResult(False, "Agent report could not be saved.", status="failed")
            return result(report)

    return ToolDefinition(
        name="agent_report",
        description="Report a phase change, result, blocker or question for your current assignment.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": sorted(REPORT_STATES)},
                "summary": {"type": "string", "maxLength": SUMMARY_LIMIT},
                "question": {"type": "string", "maxLength": SUMMARY_LIMIT},
                "required": {"type": "boolean"},
            },
            "required": ["status", "summary"],
            "additionalProperties": False,
        },
        validator=validate,
        run=run,
        capabilities={ToolCapability.SESSION_METADATA},
        affects_workspace=False,
    )
