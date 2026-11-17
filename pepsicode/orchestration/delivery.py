"""Commit coordinator input and its delivery receipts in the same snapshot."""

from __future__ import annotations

import copy

from pepsicode.core.task_control import edit_control

_BATCH_LIMIT = 8
_CONTEXT_LIMIT = 12_000


def deliver_pending(session, *, batch_limit=_BATCH_LIMIT, context_limit=_CONTEXT_LIMIT):
    """Append current-revision events to history and return their message IDs.

    Called by the session owner at a coordinator turn boundary. Callers must
    use the updated session history when constructing the coordinator request.
    ``batch_limit`` and ``context_limit`` come from the session's
    ``orchestration.*`` settings and bound one coordinator input batch.
    """
    if type(batch_limit) is not int or batch_limit < 1:
        raise ValueError("batch_limit must be a positive integer")
    if type(context_limit) is not int or context_limit < 1:
        raise ValueError("context_limit must be a positive integer")
    with session._tree_lock:
        state = session.task_control.get("orchestration") or {}
        pending = [event for event in state.get("events", []) if event["state"] == "pending"]
        if not pending:
            return []
        previous_messages = session.messages
        previous_tree = session.tree
        session.messages = copy.deepcopy(previous_messages)
        session.tree = copy.deepcopy(previous_tree)
        delivered = []
        try:
            with edit_control(session=session) as control:
                task = control.get("task") or {}
                work = {item["attempt_id"]: item for item in state["work"]}
                remaining = context_limit
                for event in pending:
                    if (event["task_id"], event["revision"]) != (task.get("id"), task.get("revision")):
                        event["state"] = "stale"
                        continue
                    if task.get("status") not in {"active", "waiting_user"}:
                        continue
                    item = work[event["attempt_id"]]
                    content = (
                        f"Worker result data for attempt {event['attempt_id']}, "
                        f"revision {event['revision']}: {event['kind']}.\n"
                        f"{item.get('summary', '')}"
                    )
                    if len(delivered) == batch_limit or remaining < len(content):
                        break
                    message_id = f"orchestration:{event['event_id']}"
                    session.messages.append(
                        {
                            "role": "user",
                            "internal": True,
                            "source": {"kind": "orchestration-event", "id": message_id},
                            "content": content,
                        }
                    )
                    event.update(state="delivered", message_id=message_id)
                    delivered.append(message_id)
                    remaining -= len(content)
        except BaseException:
            session.messages = previous_messages
            session.tree = previous_tree
            raise
        return delivered
