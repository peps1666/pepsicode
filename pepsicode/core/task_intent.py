"""One ordered intent projection for accepted and delivered human instructions."""

from __future__ import annotations

import copy


def _project(task):
    current = copy.deepcopy(task["delivered_intent"])
    for pending in task.get("pending_intents", []):
        if pending["classification"].get("read_only_request"):
            current = pending["classification"]
    task["classification"] = copy.deepcopy(current)
    task["kind"] = current["kind"]


def apply_user_instruction(task, input_id, content, *, delivered):
    from pepsicode.core.file_contract import file_mentions
    from pepsicode.core.task_control import classify_intent

    task.setdefault("delivered_intent", copy.deepcopy(task.get("classification", {"kind": task["kind"]})))
    pending = task.setdefault("pending_intents", [])
    delivered_ids = task.setdefault("delivered_inputs", [task["inputs"][0]])
    if input_id in delivered_ids:
        return False
    proposed = {**classify_intent(content), "source": "human_instruction", "input_id": input_id}
    if input_id not in task["inputs"]:
        task["revision"] += 1
        task["inputs"].append(input_id)
        task["instructions"].append(content)
        task.setdefault("request_sources", []).append({"id": input_id, "text": content, "source": "human"})
        if task.get("acceptance"):
            task.setdefault("acceptance_history", []).append(
                {"acceptance": task["acceptance"], "evidence": task.get("evidence", [])}
            )
        task.update(evidence=[], acceptance=None, status="active")
    if not delivered:
        if not any(item["id"] == input_id for item in pending):
            pending.append({"id": input_id, "classification": proposed})
        _project(task)  # Pending input may narrow, never widen.
        return True
    before = copy.deepcopy(task["delivered_intent"])
    # A supplemental fact or a plain "continue" cannot remove restrictions.
    if proposed["read_only_request"] or proposed["kind"] != "answer":
        task["delivered_intent"] = proposed
    task.setdefault("classification_history", []).append(
        {
            "before": before,
            "after": copy.deepcopy(task["delivered_intent"]),
            "input_id": input_id,
            "revision": task["revision"],
        }
    )
    task["pending_intents"] = [item for item in pending if item["id"] != input_id]
    delivered_ids.append(input_id)
    task["last_delivered_input_id"] = input_id
    additions = file_mentions(content, input_id)
    paths = {item["path"] for item in additions if item["role"] != "reference"}
    existing_paths = {item["path"] for item in task.get("file_mentions", [])}
    task["file_mentions"] = [item for item in task.get("file_mentions", []) if item["path"] not in paths] + [
        item for item in additions if item["role"] != "reference" or item["path"] not in existing_paths
    ]
    # New output inference can add a constraint; input/delete/reference roles
    # never manufacture an existence requirement. Explicit constraints survive.
    task["required_artifacts"] = [
        item
        for item in task.get("required_artifacts", [])
        if item["path"] not in paths or item.get("source") != "inferred"
    ] + [item for item in additions if item["role"] == "output" and item["confirmed"]]
    _project(task)
    if not task["pending_intents"]:
        task["admitted_revision"] = task["revision"]
    return True


def return_user_instructions(task, input_ids):
    pending = task.get("pending_intents", [])
    remaining = [item for item in pending if item["id"] not in input_ids]
    if len(remaining) == len(pending):
        return
    task["pending_intents"] = remaining
    task.setdefault("returned_inputs", []).extend(item["id"] for item in pending if item["id"] in input_ids)
    _project(task)
    if not remaining:
        task["admitted_revision"] = task["revision"]
