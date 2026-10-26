"""Session-owned task state. Persistence precedes acknowledgements and effects."""

from __future__ import annotations

import copy
import re
import threading
import uuid
from contextlib import contextmanager

from pepsicode.core.session_tree import current_session
from pepsicode.execution.runtime import current_scope

_fallback_lock = threading.RLock()


def owner(context=None):
    scope = (getattr(context, "execution", None) if context else None) or current_scope()
    if scope and scope.parent_task_id is not None:
        return None, scope.control
    session = current_session()
    if session is not None:
        return session, session.task_control
    if scope and (scope.budget or scope.journal or scope.control):
        return None, scope.control
    if context is not None:
        return None, context.control
    if scope:
        return None, scope.control
    return None, {}


def validate_control(control):
    """Validate the durable/wire boundary before installing recovered state."""
    if not isinstance(control, dict) or isinstance(control.get("version", 1), bool) or control.get("version", 1) != 1:
        raise ValueError("Unsupported task control state")
    task = control.get("task")
    if task is not None:
        if not isinstance(task, dict) or not all(
            isinstance(task.get(key), str) and task[key] for key in ("id", "objective")
        ):
            raise ValueError("Invalid persisted task identity")
        if isinstance(task.get("revision"), bool) or not isinstance(task.get("revision"), int) or task["revision"] < 1:
            raise ValueError("Invalid persisted task revision")
        if task.get("kind") not in {"answer", "change", "operation"} or task.get("status") not in {
            "active",
            "answered",
            "complete",
            "incomplete",
            "waiting_user",
            "cancelled",
            "blocked",
            "unknown",
            "budget_exhausted",
        }:
            raise ValueError("Invalid persisted task phase")
        if not all(isinstance(task.get(key), list) for key in ("inputs", "instructions", "criteria", "evidence")):
            raise ValueError("Invalid persisted task collections")
        if not all(isinstance(value, str) for key in ("inputs", "instructions") for value in task[key]):
            raise ValueError("Invalid persisted task input")
        from pepsicode.core.task_acceptance import validate_criteria

        validate_criteria(task["criteria"])
    inbox = control.get("inbox", {})
    if not isinstance(inbox, dict) or not all(
        isinstance(inbox.get(key, []), list) for key in ("pending", "claimed", "returned")
    ):
        raise ValueError("Invalid persisted inbox")
    for key in ("pending", "claimed", "returned"):
        from pepsicode.core.input_queue import QueuedInput

        for item in inbox.get(key, []):
            QueuedInput(**item)
    if not isinstance(control.get("todos", []), list) or not isinstance(control.get("questions", []), list):
        raise ValueError("Invalid persisted task metadata")
    for item in control.get("todos", []):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("content"), str)
            or item.get("status") not in {"pending", "in_progress", "completed"}
        ):
            raise ValueError("Invalid persisted todo")
    for item in control.get("questions", []):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("question"), str)
            or item.get("status") not in {"pending", "answered"}
            or not isinstance(item.get("required"), bool)
        ):
            raise ValueError("Invalid persisted question")


@contextmanager
def edit_control(context=None, session=None):
    from pepsicode.core.session import save_session

    if session is None:
        session, control = owner(context)
    else:
        control = session.task_control
    with session._tree_lock if session else _fallback_lock:
        before = copy.deepcopy(control)
        try:
            yield control
            control.setdefault("version", 1)
            if session:
                save_session(session)
        except BaseException:
            control.clear()
            control.update(before)
            raise


def snapshot(context=None):
    session, control = owner(context)
    with session._tree_lock if session else _fallback_lock:
        return copy.deepcopy(control)


def classify_intent(text):
    # This is a scheduling hint, never an authorization decision. A caller can
    # supply taskKind; the model can strengthen the contract with task_state.
    only = bool(
        re.search(
            r"只(?:需|要)?(?:解释|分析|审查)|仅(?:解释|分析)|不要(?:改|写)(?:动)?(?:任何)?(?:代码|文件)|do not (?:edit|modify)(?: any)? (?:files|code)|explain only",
            text,
            re.I,
        )
    )
    mixed = bool(
        re.search(
            r"(?:再|并|然后|接着).*(?:改|实现|做|添加|修复)|\b(?:and|then)\s+(?:fix|implement|change|create)",
            text,
            re.I,
        )
    )
    normalized = re.sub(r"^(?:请你|请|帮我|帮忙|麻烦你)[，,\s]*", "", text.strip())
    question = bool(
        re.search(
            r"^(?:如何|怎么|为什么|解释|分析|审查|对比|what\b|why\b|how\b|explain\b|review\b|compare\b)",
            normalized,
            re.I,
        )
    )
    action = bool(
        re.search(
            r"实现|修复|修改|改一下|改一改|添加|新增|编写|部署|执行|运行|完成|帮我|帮忙|请把|做成|\b(?:implement|fix|create|add|build|update|change|deploy|execute|run|support)\b",
            text,
            re.I,
        )
    )
    kind = "answer" if only or (question and not mixed) else "change" if action or mixed else "answer"
    explicit_action = bool(
        re.search(
            r"^(?:实现|修复|修改|添加|新增|支持|做成|把|implement\b|fix\b|create\b|add\b|support\b)|请把|\bcan you\s+(?:fix|implement|create)",
            normalized,
            re.I,
        )
    )
    return {
        "kind": kind,
        "source": "rule",
        "locked": only or (kind == "change" and (explicit_action or mixed)),
        "read_only_request": only,
    }


def infer_kind(text):
    return classify_intent(text)["kind"]


def is_continuation(text):
    return bool(
        re.fullmatch(r"\s*(?:继续|继续执行|接着做|continue|resume)[.!。！\s]*", text, re.I)
        or re.match(r"\s*继续(?:处理|完成|执行|推进)(?:当前|这个|之前)任务", text)
    )


def artifact_requirements(text):
    # Explicit filename mentions are inferred, never represented as user-supplied structured requirements.
    paths = re.findall(
        r"(?<![\w/])([\w./-]+\.(?:py|tsx?|jsx?|md|txt|csv|json|ya?ml|html|css|png|svg|pdf|docx))\b", text
    )
    return [{"path": path, "source": "inferred"} for path in dict.fromkeys(paths) if not path.startswith(("/", "../"))]


def begin_task(messages, runtime):
    human = next((m for m in reversed(messages) if m.get("role") == "user" and not m.get("internal")), None)
    if human is None:
        return None
    source = human.setdefault("source", {"kind": "human", "id": uuid.uuid4().hex})
    input_id = source.setdefault("id", uuid.uuid4().hex)
    with edit_control() as control:
        task = control.get("task")
        if task and input_id in task["inputs"]:
            task["status"] = "active"
            task["evidence"] = []
            task["admitted_revision"] = task["revision"]
            return copy.deepcopy(task)
        resume = (
            task
            and source.get("mode") != "follow_up"
            and (task["status"] == "waiting_user" or is_continuation(human["content"]))
            and task["status"] not in {"complete", "answered"}
        )
        if resume:
            task["revision"] += 1
            task["inputs"].append(input_id)
            task["instructions"].append(human["content"])
            task["status"] = "active"
            task["evidence"] = []
            pending = [
                q for q in control.get("questions", []) if q["task_id"] == task["id"] and q["status"] == "pending"
            ]
            answer_to = source.get("answerTo", [])
            for question in pending:
                if question["id"] in answer_to or (len(pending) == 1 and not answer_to):
                    question.update(status="answered", answer=human["content"], answer_input_id=input_id)
        else:
            if task:
                control.setdefault("task_history", []).append(copy.deepcopy(task))
            kind = runtime.get("taskKind") or infer_kind(human["content"])
            if kind not in {"answer", "change", "operation"}:
                raise ValueError("taskKind must be answer, change, or operation")
            task = {
                "id": uuid.uuid4().hex,
                "revision": 1,
                "objective": human["content"],
                "kind": kind,
                "status": "active",
                "inputs": [input_id],
                "instructions": [],
                "criteria": [],
                "criteria_revision": 0,
                "evidence": [],
                "classification": {
                    **classify_intent(human["content"]),
                    **({"kind": kind, "source": "caller", "locked": True} if runtime.get("taskKind") else {}),
                },
                "required_artifacts": artifact_requirements(human["content"]) if kind != "answer" else [],
                "request_sources": [{"id": input_id, "text": human["content"], "source": "human"}],
            }
            control["task"] = task
            artifacts = task["required_artifacts"]
            if len(artifacts) == 1 and re.search(r"\.(?:md|txt|png|svg|pdf|docx|csv)$", artifacts[0]["path"], re.I):
                task["criteria"] = [{"id": "requested-artifact", "kind": "file_exists", "path": artifacts[0]["path"]}]
                task["criteria_revision"] = task["revision"]
                task["criterion_sources"] = {
                    "requested-artifact": {
                        "source": "inferred",
                        "input_id": input_id,
                        "purpose": "Check the requested artifact exists",
                    }
                }
        task["admitted_revision"] = task["revision"]
        return copy.deepcopy(task)


def admit_steering(items):
    with edit_control() as control:
        task = control.get("task")
        if not task:
            return
        for item in items:
            if item.mode != "steer":
                continue
            if item.id not in task["inputs"]:
                task["revision"] += 1
                task["inputs"].append(item.id)
                task["instructions"].append(item.content)
                task["evidence"] = []
                task["status"] = "active"
            if infer_kind(item.content) == "change":
                task["kind"] = "change"
        task["admitted_revision"] = task["revision"]


def task_context():
    state = snapshot()
    task = state.get("task")
    if not task:
        return None
    import json

    view = {key: value for key, value in task.items() if key not in {"evidence", "acceptance_changes"}}
    view["evidence"] = [
        {key: value for key, value in item.items() if key not in {"output", "argv"}} for item in task["evidence"]
    ]
    return {
        "role": "system",
        "internal": True,
        "source": {"kind": "task-policy"},
        "content": "Current task contract: "
        + json.dumps(view, ensure_ascii=False)
        + "\nFor change/operation tasks use task_state to define concrete acceptance criteria before claiming completion. "
        "Use file_contains for expected content, file_exists for required artifacts, or command for a targeted behavior check. "
        "Project tests alone do not prove this objective. Read the current task id/revision before updating. "
        "Do not downgrade explicit actionable requests or discard existing requirements; classify may correct uncertain rule inference with a reason. When a human changes requirements, "
        "task_state revise must cite the latest inputs ID and explain the changes. Reconfirm acceptance for the current revision. "
        "Required unanswered questions block completion. Classification is a scheduling hint, never permission. "
        "Use task_state classify with a reason to correct an uncertain rule inference during this normal request. "
        "For behavior changes prefer a targeted command check; static file checks only demonstrate artifacts. "
        "Do not request user approval for routine acceptance definitions or uncertain wording; investigate first.",
    }
