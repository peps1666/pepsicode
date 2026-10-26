"""Task acceptance is separate from project regression checks and normal turn exit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from functools import wraps
from tempfile import TemporaryDirectory

from pepsicode.core.loop_engine import VerificationResult
from pepsicode.core.task_control import edit_control, snapshot
from pepsicode.execution.filesystem import FILES
from pepsicode.execution.runtime import current_scope
from pepsicode.tooling import ToolContext
from pepsicode.tools.run_command import run_command_tool


def settle_task_failure(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            from pepsicode.cancellation import TurnCancelledError
            from pepsicode.execution.runtime import ModelRequestLimitError
            from pepsicode.llm.cost_tracker import BudgetExceededError

            with edit_control() as control:
                task = control.get("task")
                if task:
                    scope = current_scope()
                    task["status"] = (
                        "unknown"
                        if scope and scope.has_unknown
                        else "budget_exhausted"
                        if isinstance(error, (BudgetExceededError, ModelRequestLimitError))
                        else "cancelled"
                        if isinstance(error, (TurnCancelledError, KeyboardInterrupt))
                        else "incomplete"
                    )
                    if isinstance(kwargs.get("runtime"), dict):
                        kwargs["runtime"]["_lastTask"] = json.loads(json.dumps(task))
            raise

    return wrapped


class TaskVerifier:
    def __init__(self, project, runtime):
        self.project = project
        self.runtime = runtime

    def verify(self, *, messages, tools, cwd, permissions):
        task = snapshot().get("task")
        ref = (task["id"], task["revision"]) if task else None
        report = self.project.verify(messages=messages, tools=tools, cwd=cwd, permissions=permissions)
        self.runtime["_lastProjectVerification"] = asdict(report)
        if not task:
            return report
        current = snapshot().get("task")
        if current and (
            (current["id"], current["revision"]) != ref or current.get("admitted_revision") != current["revision"]
        ):
            return VerificationResult(
                False,
                "New steering invalidated the verification revision",
                status="failed",
                reason_code="stale_task_revision",
            )
        scope = current_scope()
        if scope and scope.has_unknown:
            return VerificationResult(
                False,
                "Reconcile unknown effects before task acceptance",
                status="blocked",
                reason_code="unknown_outcome",
            )
        if report.status in {"failed", "blocked", "cancelled"}:
            return report
        state = snapshot()
        if any(
            q["task_id"] == task["id"] and q.get("required", True) and q["status"] == "pending"
            for q in state.get("questions", [])
        ):
            return VerificationResult(
                False, "Required user question remains unanswered", status="blocked", reason_code="awaiting_user"
            )
        if task["kind"] == "answer" and not task["criteria"]:
            with edit_control() as state:
                latest = state["task"]
                if (latest["id"], latest["revision"]) != ref or latest.get("admitted_revision") != latest["revision"]:
                    return VerificationResult(
                        False,
                        "New steering invalidated the answer revision",
                        status="failed",
                        reason_code="stale_task_revision",
                    )
                state["task"]["status"] = "answered"
            return report
        if any(item.get("task_id") == task["id"] and item["status"] != "completed" for item in state.get("todos", [])):
            return VerificationResult(
                False,
                "The current task still has unfinished todos",
                "Finish the remaining work and update its todos; acceptance will be checked afterwards.",
                status="failed",
                reason_code="pending_todos",
            )
        if not task["criteria"]:
            return VerificationResult(
                False,
                "The requested task has no acceptance conditions yet",
                "Call task_state with the current task id/revision and define concrete conditions tied to the user's objective; a plan or unrelated green tests are insufficient.",
                status="failed",
                reason_code="task_incomplete",
            )
        if task.get("criteria_revision") != task["revision"]:
            return VerificationResult(
                False,
                "Review acceptance against the new user instructions",
                "Inspect task_state and define the conditions for this exact revision; earlier evidence cannot accept changed requirements.",
                status="failed",
                reason_code="stale_acceptance",
            )
        context = ToolContext(
            cwd, permissions=permissions, execution=scope, cancellation_event=self.project.cancellation_event
        )
        evidence = []
        for criterion in task["criteria"]:
            item = {"criterion_id": criterion["id"], "task_revision": task["revision"], "status": "unknown"}
            try:
                if criterion["kind"].startswith("file_"):
                    file = FILES.snapshot(context, criterion["path"], observe=False)
                    passed = file.content is not None
                    if passed and criterion["kind"] == "file_contains":
                        passed = criterion["text"] in file.text
                    item.update(
                        status="satisfied" if passed else "unsatisfied", path=str(file.path), version=file.version
                    )
                else:
                    # The normal registry supplies approvals, cancellation,
                    # hooks, durable dispatch and truthful unknown outcomes.
                    import os

                    from pepsicode.core.verification import execution_argv
                    from pepsicode.tooling import ToolRegistry

                    command_tools = tools if tools.find("run_command") else ToolRegistry([run_command_tool])
                    context.command_is_argv = True
                    context.hooks = self.project.hook_engine
                    argv = execution_argv(criterion["argv"], windows=os.name == "nt")
                    with TemporaryDirectory(prefix="pepsi-accept-cache-") as cache:
                        context.environment = {"PYTHONPYCACHEPREFIX": cache, "PYTHONDONTWRITEBYTECODE": "1"}
                        result = command_tools.execute(
                            "run_command",
                            {"command": argv[0], "args": argv[1:], "timeout": 30},
                            context,
                        )
                    item.update(
                        status="satisfied" if result.ok else "unsatisfied",
                        argv=criterion["argv"],
                        exit_code=result.exit_code,
                        output=result.output[:2000],
                        output_sha256=hashlib.sha256(result.output.encode()).hexdigest(),
                    )
                    if result.status in {"blocked", "unknown", "cancelled", "timed_out"}:
                        item["status"] = "unknown"
            except (OSError, ValueError) as error:
                item.update(status="unknown", error=str(error))
            evidence.append(item)
        # Commands and human steering can change the world while acceptance
        # runs. No earlier file result or task revision may silently survive.
        for item in evidence:
            if "path" in item:
                try:
                    if FILES.snapshot(context, item["path"], observe=False).version != item["version"]:
                        item.update(status="unknown", error="File changed during acceptance")
                except (OSError, ValueError) as error:
                    item.update(status="unknown", error=str(error))
        with edit_control() as control:
            latest = control["task"]
            stale = (latest["id"], latest["revision"]) != ref or latest.get("admitted_revision") != latest["revision"]
            if stale:
                latest["evidence"] = []
                return VerificationResult(
                    False,
                    "Task changed during verification; admit the new instruction and recheck",
                    status="failed",
                    reason_code="stale_task_revision",
                )
            latest["evidence"] = evidence
            passed = all(item["status"] == "satisfied" for item in evidence)
            latest["status"] = "complete" if passed else "active"
        if not passed:
            return VerificationResult(
                False,
                "Task acceptance conditions are not satisfied",
                json.dumps(evidence, ensure_ascii=False),
                status="failed",
                reason_code="task_incomplete",
            )
        return VerificationResult(
            True,
            "Task acceptance conditions passed",
            json.dumps({"acceptance": evidence, "project": asdict(report)}, ensure_ascii=False),
            status="passed",
            reason_code="task_acceptance",
        )


def settle_task(messages, runtime, stop_reason):
    scope = current_scope()
    with edit_control() as control:
        task = control.get("task")
        if not task:
            return
        last = messages[-1] if messages else {}
        if scope and scope.has_unknown:
            task["status"] = "unknown"
        elif scope and scope.cancellation_event and scope.cancellation_event.is_set():
            task["status"] = "cancelled"
        elif last.get("awaitingUser"):
            task["status"] = "waiting_user"
        elif last.get("isError"):
            task["status"] = "incomplete"
        elif task["status"] not in {"complete", "answered"}:
            task["status"] = "blocked" if stop_reason == "blocked" else "incomplete"
        runtime["_lastTask"] = json.loads(json.dumps(task))
        for message in reversed(messages):
            if message.get("role") == "assistant":
                message["task"] = runtime["_lastTask"]
                break
