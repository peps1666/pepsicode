from __future__ import annotations

import json

from pepsicode.agents.status import session_trace_view, trace_view
from pepsicode.core.session_tree import current_session
from pepsicode.core.task_control import snapshot
from pepsicode.tooling import ToolCapability, ToolDefinition, ToolResult


def _orchestration_work(control, *, trace_id, offset, limit):
    task = control.get("task") or {}
    work = (control.get("orchestration") or {}).get("work", [])
    matches = [
        item
        for item in work
        if item["task_id"] == task.get("id") and (trace_id is None or item["trace_id"] == trace_id)
    ]
    return {
        "records": [
            {
                **{key: item[key] for key in ("work_id", "attempt_id", "trace_id", "revision", "state")},
                "stale": item["revision"] != task.get("revision"),
                "summary": item.get("summary", "")[:400],
            }
            for item in matches[offset : offset + limit]
        ],
        "next_offset": offset + limit if offset + limit < len(matches) else None,
    }


def create_agent_status(manager):
    def validate(data):
        trace_id, offset = data.get("trace_id"), data.get("offset", 0)
        if trace_id is not None and (not isinstance(trace_id, str) or not 0 < len(trace_id) <= 64):
            raise ValueError("Invalid trace_id")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        return trace_id, offset

    def run(data, context):
        trace_id, offset = data
        session = current_session()
        limit = 1 if trace_id else 5
        if session:
            with session._tree_lock:
                view = session_trace_view(session, trace_id=trace_id, offset=offset, limit=limit)
                control = session.task_control
                if "orchestration" in control:
                    view["orchestration_work"] = _orchestration_work(
                        control, trace_id=trace_id, offset=offset, limit=limit
                    )
        else:
            view = trace_view(manager, snapshot(context), trace_id=trace_id, offset=offset, limit=limit)
        for row in view["records"]:
            row["task"] = row["task"][:300]
            row["result_summary"] = row["result_summary"][:400]
            if row["end_reason"]:
                row["end_reason"] = row["end_reason"][:400]
            row["questions"] = row["questions"][-3:]
            row["activities"] = row["activities"][-3:]
            if not trace_id:
                row.pop("recent_tools")
            for question in row["questions"]:
                question["question"] = question["question"][:240]
            if row["report"]:
                row["report"]["summary"] = row["report"]["summary"][:400]
        return ToolResult(True, json.dumps(view, ensure_ascii=False))

    return ToolDefinition(
        name="agent_status",
        description="List Agent assignments in this session or look up an exact trace ID. Results are paged.",
        input_schema={
            "type": "object",
            "properties": {"trace_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
        },
        validator=validate,
        run=run,
        capabilities={ToolCapability.READ_ONLY},
        concurrency_safe=True,
        affects_workspace=False,
        always_loaded=True,
        max_result_size_chars=16_000,
    )
