"""Host activity and the shared Agent status projection."""

from __future__ import annotations

import json
from dataclasses import asdict

from pepsicode.agents.trace import TRACE_VERSION, Activity, TraceNode

PAGE_SIZE = 20


def tool_summary(tool: str, arguments: dict) -> str:
    if tool == "run_command":
        return f"Running {arguments.get('command', 'command')}"[:240]
    path = arguments.get("path") or arguments.get("file_path")
    if tool == "read_file":
        return f"Reading {path or 'file'}"[:240]
    return f"{tool} {path or ''}".strip()[:240]


def observe_activity(manager, trace_id, event, data):
    if event in {"tool/call", "tool/result"}:
        call_id = data["call_id"]
        tool = data["tool"]
        if event == "tool/call":
            manager.record_tool_call(trace_id)
            activity = Activity(
                call_id,
                "tool",
                tool_summary(tool, data["input"]),
                tool=tool,
                input_preview=json.dumps(data["input"], ensure_ascii=False)[:500],
            )
        else:
            manager.finish_tool(
                trace_id,
                call_id,
                tool,
                data["status"],
                data["output"],
                data["duration_ms"],
            )
            return
    elif event.startswith("model/") and "request_id" in data:
        call_id = data["request_id"]
        if usage := data.get("usage"):
            manager.record_tokens(trace_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        activity = Activity(call_id, "model", "Waiting for model response") if event == "model/prepared" else None
    elif event in {"process/started", "background/end"}:
        if event == "process/started" and not data["persistent"]:
            return
        call_id = f"background:{data['pid']}"
        activity = (
            Activity(call_id, "background", f"Waiting for background process {data['pid']}")
            if event == "process/started"
            else None
        )
    elif "activity_id" in data:
        call_id = data["activity_id"]
        kind = event.split("/", 1)[0]
        summary = {
            "model": "Waiting for model",
            "approval": "Waiting for approval",
            "retry": "Waiting to retry model request",
        }[kind]
        activity = Activity(call_id, kind, summary) if event.endswith("/start") else None
    else:
        return
    manager.activity(trace_id, call_id, activity)


def project(
    node: TraceNode, task: dict, questions: list[dict], *, active_branch=True, recovery=False, detail=False
) -> dict:
    matching_task = (node.parent_task_id, node.spawn_revision) == (task.get("id"), task.get("revision"))
    stale = not active_branch or not matching_task
    report = node.reports[-1] if node.reports else None
    applicable_report = report if report and not report.stale and not stale else None
    questions = [q for q in questions if q.get("trace_id") == node.trace_id]
    pending = [
        q for q in questions if q["status"] == "pending" and active_branch and q.get("task_id") == task.get("id")
    ]
    required = any(q["required"] for q in pending)
    activities = node.activities[-12:]
    criteria = task.get("criteria", []) if not stale else []
    proof = task.get("acceptance") or {}
    run_id = proof.get("acceptance_run_id")
    workspace_digest = proof.get("workspace", {}).get("digest")
    current_proof = (
        not stale
        and proof.get("valid")
        and proof.get("task_revision") == node.spawn_revision
        and run_id
        and workspace_digest
    )
    passed = {
        item.get("criterion_id")
        for item in task.get("evidence", [])
        if current_proof
        and item.get("status") == "satisfied"
        and item.get("task_revision") == node.spawn_revision
        and item.get("acceptance_run_id") == run_id
        and item.get("workspace_digest") == workspace_digest
    } & {item["id"] for item in criteria}
    verified = task.get("status") == "complete" and current_proof and len(passed) == len(criteria)
    if recovery or node.status == "interrupted":
        stage = "recovery_pending"
    elif node.ended_at is not None and node.status not in {"completed", "awaiting_user"}:
        stage = "budget_exhausted" if node.end_reason == STEP_LIMIT_MESSAGE else node.status
    elif required:
        stage = "waiting_user"
    elif any(item.kind == "approval" for item in activities):
        stage = "waiting_approval"
    elif activities:
        stage = "running"
    elif applicable_report and applicable_report.status == "ready":
        stage = "verified" if verified else "pending_verification"
    elif applicable_report and applicable_report.status in {"blocked", "failed", "dropped"}:
        stage = f"reported_{applicable_report.status}"
    elif node.status == "awaiting_user" and not required:
        stage = "awaiting_continuation" if active_branch else "stale_result"
    elif node.status == "completed" and stale:
        stage = "stale_result"
    else:
        stage = node.status if node.status != "running" else "idle"
    return {
        "trace_id": node.trace_id,
        "parent_id": node.parent_id,
        "session_id": node.session_id,
        "name": node.name,
        "agent_type": node.agent_type,
        "task": node.task if detail else node.task[:600],
        "parent_task_id": node.parent_task_id,
        "spawn_revision": node.spawn_revision,
        "execution_scope_id": node.execution_scope_id,
        "parent_call_id": node.parent_call_id,
        "branch_entry_id": node.branch_entry_id,
        "historical": not active_branch,
        "stale": stale,
        "stage": stage,
        "status": node.status,
        "started_at": node.started_at,
        "ended_at": node.ended_at,
        "elapsed_seconds": node.elapsed_seconds,
        "last_activity_at": node.last_activity_at,
        "end_reason": node.end_reason if detail or not node.end_reason else node.end_reason[:600],
        "result_summary": node.result_summary if detail else node.result_summary[:600],
        "activities": [
            {
                "id": item.id,
                "kind": item.kind,
                "summary": item.summary,
                "started_at": item.started_at,
                "tool": item.tool,
            }
            for item in activities
        ],
        "report": {**asdict(report), "summary": report.summary if detail else report.summary[:600]} if report else None,
        "questions": [
            {
                **{key: q.get(key) for key in ("id", "required", "status", "answer_input_id")},
                "question": q["question"] if detail else q["question"][:240],
                "actionable": q in pending,
            }
            for q in (questions if detail else questions[-12:])
        ],
        "input_tokens": node.input_tokens,
        "output_tokens": node.output_tokens,
        "tool_call_count": node.tool_call_count,
        "recent_tools": [asdict(item) for item in node.recent_tools],
        "acceptance": {
            "task_status": task.get("status") if matching_task and active_branch else None,
            "passed": len(passed),
            "total": len(criteria),
            "basis": "parent_task_criteria",
        },
    }


def trace_view(
    manager, control, *, branch=None, unresolved=(), trace_id=None, offset=0, limit=PAGE_SIZE, live_first=False
):
    def on_branch(node):
        return branch is None or node.branch_entry_id is None or node.branch_entry_id in branch

    sequence, nodes = manager.snapshot(include_history=False)
    task = control.get("task") or {}
    questions = control.get("questions", [])
    work = (control.get("orchestration") or {}).get("work", [])
    current_work = {
        row["trace_id"]: row
        for row in work
        if (row["task_id"], row["revision"]) == (task.get("id"), task.get("revision"))
    }
    if trace_id:
        node = manager.get(trace_id)
        nodes = [node] if node else []
    else:
        if not live_first and manager.has_history and sum(on_branch(node) for node in nodes) <= offset + limit:
            sequence, nodes = manager.snapshot()
    represented = {node.trace_id for node in nodes}
    queued = set()
    for reserved_id, item in current_work.items():
        if (
            (trace_id and trace_id != reserved_id)
            or reserved_id in represented
            or item["state"] not in {"queued", "starting", "recovery_pending", "cancelled", "failed"}
        ):
            continue
        spec = item["specification"]
        queued.add(reserved_id)
        nodes.append(
            TraceNode(
                trace_id=reserved_id,
                parent_id=manager.session_id,
                session_id=manager.session_id,
                agent_type=spec.get("agent_type", "worker"),
                name=spec.get("agent_type", "Worker"),
                parent_task_id=item["task_id"],
                spawn_revision=item["revision"],
                task=spec.get("goal", ""),
                status=item["state"],
                started_at=item.get("queued_at", 0),
                ended_at=item.get("settled_at", 0) if item["state"] in {"cancelled", "failed"} else None,
                result_summary=item.get("summary", ""),
            )
        )
    if not trace_id:
        nodes.sort(key=lambda node: (on_branch(node), live_first and node.ended_at is None, node.started_at))
    page = list(reversed(nodes))[offset : offset + limit]
    records = []
    for node in page:
        row = project(
            node,
            task,
            questions,
            active_branch=on_branch(node),
            recovery=node.trace_id in unresolved,
            detail=bool(trace_id),
        )
        if node.trace_id in queued:
            row.update(started_at=0, elapsed_seconds=0)
        work_state = current_work.get(node.trace_id, {}).get("state")
        if work_state == "stop_requested":
            row["stage"] = "stop_requested"
        if work_state in {"queued", "starting"}:
            row["can_stop"] = True
        if work_state == "waiting_user":
            row["can_reply_running"] = True
        records.append(row)
    return {
        "version": TRACE_VERSION,
        "session_id": manager.session_id,
        "sequence": sequence,
        "records": records,
        "next_offset": offset + limit if offset + limit < len(nodes) else None,
        "persistence_error": manager.persistence_error,
    }


def session_trace_view(session, **params):
    from pepsicode.core.session_tree import branch_path

    with session._tree_lock:
        return trace_view(
            session.agent_traces,
            session.task_control,
            branch={item["id"] for item in branch_path(session)},
            unresolved=(session.execution_state or {}).get("unknown_trace_ids", []),
            **params,
        )


STEP_LIMIT_MESSAGE = "Reached the maximum tool step limit for this turn."
STAGE_LABELS = {
    "queued": "排队中",
    "stop_requested": "正在停止",
    "starting": "正在启动",
    "idle": "等待下一步",
    "running": "运行中",
    "completed": "已返回",
    "waiting_user": "等待你回复",
    "awaiting_continuation": "等待继续",
    "waiting_approval": "等待审批",
    "pending_verification": "结果待核验",
    "verified": "父任务验收通过",
    "reported_blocked": "报告受阻",
    "reported_failed": "报告失败",
    "reported_dropped": "报告放弃",
    "stale_result": "旧版本结果",
    "recovery_pending": "中断待核对",
    "unknown": "结果未知",
    "cancelled": "已停止",
    "timed_out": "超时",
    "failed": "执行失败",
    "blocked": "执行受阻",
    "budget_exhausted": "达到执行限制",
}


def display_text(text: str, limit: int = 240) -> str:
    text = " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def assignment_summary(task: str) -> str:
    for line in task.splitlines():
        clean = line.strip().lstrip("# ")
        if clean.startswith(("调查主题", "调查目标", "目标：", "目标:", "负责事项", "任务：", "任务:")):
            return display_text(clean, 160)
    return display_text(task, 160)


def stage_label(row: dict) -> str:
    if row["stage"] in {"failed", "budget_exhausted"} and row.get("end_reason") == STEP_LIMIT_MESSAGE:
        return "达到轮数上限"
    return STAGE_LABELS.get(row["stage"], row["stage"])


def report_summary(row: dict) -> str:
    text = (row.get("report") or {}).get("summary") or row.get("result_summary") or ""
    return "" if text == row.get("end_reason") else display_text(text)


def format_trace_view(view: dict) -> str:
    lines = []
    for row in view["records"]:
        line = (
            f"{display_text(row['name'])} [{row['trace_id']}] · {stage_label(row)} · {round(row['elapsed_seconds'])}秒"
        )
        lines.extend([line, "负责：" + assignment_summary(row["task"])])
        lines.extend("当前：" + display_text(item["summary"]) for item in row["activities"][-2:])
        summary = report_summary(row)
        if summary:
            lines.append("最近报告：" + summary)
        reason = row["end_reason"]
        if (
            reason
            and reason not in {STEP_LIMIT_MESSAGE, "Child returned a response."}
            and display_text(reason) != summary
        ):
            lines.append("结束原因：" + display_text(reason))
        lines.extend(f"待回复 [{q['id']}]：{display_text(q['question'])}" for q in row["questions"] if q["actionable"])
        acceptance = row["acceptance"]
        if acceptance["total"]:
            lines.append(f"父任务验收：{acceptance['passed']}/{acceptance['total']}")
        lines.append("")
    if not lines:
        lines.append("当前会话还没有子 Agent 执行记录。")
    if view["persistence_error"]:
        lines.append(f"追踪状态保存失败：{display_text(view['persistence_error'])}")
    return "\n".join(lines).rstrip()
