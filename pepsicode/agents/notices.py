"""Deliver durable Worker notices at the parent loop's model boundary."""

from pepsicode.core.session_tree import branch_path, current_session
from pepsicode.execution.runtime import current_scope


def deliver_agent_notices(messages):
    session = current_session()
    scope = current_scope()
    if session is None or (scope and scope.trace_id) or not session.agent_traces.has_records():
        return
    from pepsicode.core.session import save_session

    with session._tree_lock:
        branch = branch_path(session)
        ancestors = {entry["id"] for entry in branch}
        delivered = {
            message.get("source", {}).get("id")
            for message in [*(entry["message"] for entry in branch), *messages]
            if message.get("source", {}).get("kind") == "agent-notice"
        }
        delivered_results = {
            message["source"].get("trace_id")
            for message in messages
            if message.get("source", {}).get("kind") == "orchestration-event"
        }
        task = session.task_control.get("task") or {}
        questions = {q["id"]: q for q in session.task_control.get("questions", [])}
        notices = []

        def enqueue(notice_id, node, text):
            if notice_id not in delivered and len(notices) < 8:
                notices.append(
                    {
                        "role": "user",
                        "internal": True,
                        "source": {"kind": "agent-notice", "id": notice_id},
                        "content": f"Worker report data from {node.name} [{node.trace_id}], task {node.parent_task_id}, "
                        f"revision {node.spawn_revision}. Cite the trace and revision when using this result. "
                        f"Use the parent task criteria for acceptance.\n{text[:900]}",
                    }
                )

        for node in session.agent_traces.all_nodes():
            if node.parent_task_id != task.get("id") or (
                node.branch_entry_id and node.branch_entry_id not in ancestors
            ):
                continue
            for report in node.reports:
                notice_id = f"agent:{node.trace_id}:{report.id}"
                question = questions.get(report.question_id)
                if question:
                    enqueue(
                        notice_id + ":question",
                        node,
                        f"Question [{question['id']}] ({question['status']}): {question['question']}",
                    )
            report = next((item for item in reversed(node.reports) if item.status != "progress"), None)
            if node.trace_id in delivered_results:
                continue
            if node.ended_at and node.status != "awaiting_user":
                report_text = f"Worker {report.status}: {report.summary}" if report else ""
                enqueue(
                    f"agent:{node.trace_id}:ended",
                    node,
                    f"Host outcome: {node.status}. {report_text or node.result_summary or node.end_reason or ''}",
                )
            elif report:
                enqueue(f"agent:{node.trace_id}:{report.id}", node, f"{report.status}: {report.summary}")
        if not notices:
            return
        messages.extend(notices)
        previous_messages = session.messages
        session.messages = list(messages)
        try:
            save_session(session)
        except OSError as error:
            del messages[-len(notices) :]
            session.messages = previous_messages
            session.tree["leaf_id"] = branch[-1]["id"] if branch else None
            session.agent_traces.set_persistence_error(str(error))
