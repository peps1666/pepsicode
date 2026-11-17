"""Task acceptance is separate from project regression checks and normal turn exit."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory

from pepsicode.core.loop_engine import VerificationResult
from pepsicode.core.session_tree import current_session
from pepsicode.core.task_control import edit_control, snapshot
from pepsicode.core.workspace_state import digest, summary, task_paths, workspace_state
from pepsicode.execution.filesystem import FILES
from pepsicode.execution.runtime import current_scope
from pepsicode.tooling import ToolContext
from pepsicode.tools.run_command import run_command_tool

_DELIVERED_STATUSES = {"local", "delivered"}


def delivered_acceptance_root(session, task, scope):
    """Root parent acceptance at the task's delivered version when one exists.

    Returns ``(checkout, head_sha, problem)``. When the current task revision
    has a delivered integration whose record still validates (accepted,
    acceptance valid, clean checkout at the recorded HEAD, candidates intact),
    parent acceptance and required-artifact checks run against that checkout —
    the version actually delivered, not the orchestrator's working directory.

    A problem string means a delivery exists for the current revision but its
    recorded version can no longer be trusted (stale integration, failed or
    missing acceptance, unconfirmed delivery). Acceptance then fails with that
    reason instead of silently checking a different version; the coordinator
    must re-integrate, re-verify and deliver again.
    """
    if session is None or not task:
        return None, None, None
    state = (getattr(session, "task_control", None) or {}).get("orchestration") or {}
    revision = (task["id"], task["revision"])
    integrations = [
        record
        for record in (state.get("integrations") or {}).values()
        if (record.get("task_id"), record.get("revision")) == revision
    ]
    deliveries = [
        record
        for record in (state.get("deliveries") or {}).values()
        if (record.get("task_id"), record.get("revision")) == revision
    ]
    if not integrations and not deliveries:
        return None, None, None
    if not integrations:
        return (
            None,
            None,
            "A delivery exists but its integration record is missing; re-integrate and deliver the current version",
        )
    latest = None
    for record in integrations:  # insertion-ordered; a later record wins a timestamp tie
        if latest is None or (record.get("created_at") or 0.0) >= (latest.get("created_at") or 0.0):
            latest = record
    integration_deliveries = [
        record for record in deliveries if record.get("integration_id") == latest["integration_id"]
    ]
    if not any(record.get("status") in _DELIVERED_STATUSES for record in integration_deliveries):
        return None, None, "The latest integration for this task is not delivered yet; deliver it before acceptance"
    if any(record.get("status") == "uncertain" for record in integration_deliveries):
        return (
            None,
            None,
            "An external delivery for this integration has an uncertain outcome; reconcile it before acceptance",
        )
    delivery_target = (task.get("requirements") or {}).get("delivery_target")
    objective_text = "\n".join([task.get("objective", ""), *(task.get("instructions") or [])])
    if delivery_target == "merge_or_deploy" or re.search(
        r"合并\s*PR|合并到主分支|合入主分支|上线生产|部署生产|\b(?:merge\s+(?:the\s+)?(?:pr|pull\s+request)|deploy\s+to\s+prod(?:uction)?)\b",
        objective_text,
        re.I,
    ):
        if not any(
            record.get("status") == "delivered"
            and (
                (record.get("external_refs") or {}).get("merged") is True
                or (record.get("external_refs") or {}).get("deployed") is True
            )
            for record in integration_deliveries
        ):
            return (
                None,
                None,
                "Creating a pull request or local branch does not satisfy a merge or deployment objective",
            )
    elif delivery_target == "pull_request" or re.search(
        r"(?:提交|创建|发起|提一个|开一个)\s*PR\b|\b(?:open|create|submit)\s+(?:a\s+)?(?:pr|pull\s+request)\b",
        objective_text,
        re.I,
    ):
        if not any(
            record.get("kind") == "pull_request"
            and record.get("status") == "delivered"
            and (record.get("external_refs") or {}).get("pr_number")
            for record in integration_deliveries
        ):
            return None, None, "This task requires a delivered pull request before completion"
    from pepsicode.orchestration.integration import validate_integration

    try:
        record = validate_integration(session, scope, latest["integration_id"])
    except (ValueError, PermissionError, RuntimeError) as error:
        return None, None, f"The delivered version could not be revalidated: {error}"
    acceptance = record.get("acceptance") or {}
    if record.get("state") != "accepted" or not acceptance.get("valid"):
        return None, None, "The delivered version changed after delivery; re-integrate, re-verify and deliver again"
    checkout = Path(record["worktree"])
    if not checkout.is_dir():
        return None, None, "The delivered integration checkout is missing; re-integrate and deliver again"
    return checkout, record.get("head_sha"), None


def settle_task_failure(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            permissions = kwargs.get("permissions")
            if permissions and hasattr(permissions, "clear_task_grants"):
                permissions.clear_task_grants()
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
        mode = self.runtime.get("verificationMode", "always" if self.runtime.get("verifierEnabled") else "auto")
        plain_answer = (
            task
            and task["kind"] == "answer"
            and not task["criteria"]
            and not task.get("required_artifacts")
            and not task.get("requirements", {}).get("behavior")
        )
        session = current_session()
        if (
            plain_answer
            and session is not None
            and session.profile == "qq_private"
            and tools.mutation_count <= self.project.turn_start_count
        ):
            # QQ conversation makes no claim about project behavior. Prior
            # unverified work still remains pending for the next coding task;
            # it must not force repository snapshots after every chat reply.
            return self._verify(
                messages=messages,
                tools=tools,
                cwd=cwd,
                permissions=permissions,
                project_report=VerificationResult(
                    False,
                    "Conversational answer; no edits or commands in this turn",
                    status="not_run",
                    reason_code="conversation_answer",
                ),
            )
        if (
            task
            and task["kind"] == "answer"
            and task.get("classification", {}).get("read_only_request")
            and (not task["criteria"] or task.get("classification", {}).get("source") == "human_instruction")
        ) or (
            plain_answer and (mode == "off" or (mode == "auto" and tools.mutation_count <= self.project.start_count))
        ):
            # An explanation makes no workspace-behavior claim. Do not scan a
            # large or partially unreadable repository merely to answer it.
            return self._verify(messages=messages, tools=tools, cwd=cwd, permissions=permissions)
        try:
            acceptance_root, delivered_head, delivered_problem = delivered_acceptance_root(
                session, task, current_scope()
            )
            if delivered_problem is not None:
                return self._verify(
                    messages=messages,
                    tools=tools,
                    cwd=cwd,
                    permissions=permissions,
                    project_report=VerificationResult(
                        False, delivered_problem, status="failed", reason_code="delivered_version_changed"
                    ),
                )
            acceptance_cwd = str(acceptance_root) if acceptance_root is not None else cwd
            preparation = self.runtime.get("verificationPreparationCommands")
            if preparation:
                from pepsicode.core.verification import ProjectVerifier

                prepared = ProjectVerifier(
                    {**self.runtime, "verificationMode": "always", "verificationCommands": preparation},
                    tools,
                    self.project.cancellation_event,
                    hook_engine=self.project.hook_engine,
                ).verify(messages=messages, tools=tools, cwd=acceptance_cwd, permissions=permissions)
                if not prepared.ok:
                    return prepared
            before = workspace_state(acceptance_cwd, include_paths=task_paths(task or {}), reason="acceptance_start")
        except (OSError, ValueError) as error:
            return VerificationResult(
                False, str(error), status="blocked", reason_code="verification_snapshot_unavailable"
            )
        run = {
            "acceptance_run_id": uuid.uuid4().hex,
            "task_id": task["id"] if task else None,
            "task_revision": task["revision"] if task else None,
            "criteria_digest": digest(task["criteria"] if task else []),
            "requirements_digest": digest([task.get("required_artifacts", []), task.get("requirements", {})])
            if task
            else digest([]),
            "workspace": summary(before),
        }
        if acceptance_root is not None:
            run["delivered"] = {"checkout": str(acceptance_root), "head_sha": delivered_head}
        report = self._verify(
            messages=messages, tools=tools, cwd=cwd, permissions=permissions, acceptance_root=acceptance_root
        )
        with edit_control() as control:
            latest = control.get("task")
            try:
                after = workspace_state(acceptance_cwd, include_paths=task_paths(task or {}), reason="acceptance_final")
                changed = before["digest"] != after["digest"]
            except (OSError, ValueError):
                changed = True
            if (
                task
                and latest
                and (
                    (latest["id"], latest["revision"]) != (task["id"], task["revision"])
                    or digest(latest["criteria"]) != run["criteria_digest"]
                    or digest([latest.get("required_artifacts", []), latest.get("requirements", {})])
                    != run["requirements_digest"]
                )
            ):
                changed = True
            if changed:
                if latest:
                    latest.update(status="active", evidence=[], acceptance=None)
                report = VerificationResult(
                    False,
                    "Verification inputs changed; previous evidence is invalid",
                    "Move generators and fixture setup to verificationPreparationCommands, then verify a stable state.",
                    status="failed",
                    reason_code="verification_state_changed",
                )
            if latest and task and latest["id"] == task["id"]:
                if report.ok:
                    pending = any(
                        q.get("task_id") == task["id"] and q.get("required", True) and q["status"] == "pending"
                        for q in control.get("questions", [])
                    )
                    todos = any(
                        t.get("task_id") == task["id"] and t.get("required", True) and t["status"] != "completed"
                        for t in control.get("todos", [])
                    )
                    scope = current_scope()
                    if (
                        pending
                        or todos
                        or (
                            scope
                            and (scope.has_unknown or (scope.cancellation_event and scope.cancellation_event.is_set()))
                        )
                    ):
                        latest.update(status="active", evidence=[])
                        report = VerificationResult(
                            False,
                            "Task prerequisites changed during acceptance",
                            status="blocked",
                            reason_code="acceptance_prerequisite_changed",
                        )
                    else:
                        latest["status"] = "complete"
                latest["acceptance"] = {**run, "valid": report.ok, "reason_code": report.reason_code}
                for item in latest.get("evidence", []):
                    item.update(acceptance_run_id=run["acceptance_run_id"], workspace_digest=before["digest"])
        report.state = run
        # Only requested artifacts and satisfied behavior checks count as
        # progress. Logs, Todo wording and unrelated file churn do not.
        latest = snapshot().get("task") or {}
        relevant = {item["path"] for item in latest.get("required_artifacts", [])}
        relevant.update(c["path"] for c in latest.get("criteria", []) if "path" in c)
        report.progress = digest(
            [
                run["task_revision"],
                {p: before["entries"].get(p) for p in sorted(relevant)},
                sorted(e["criterion_id"] for e in latest.get("evidence", []) if e["status"] == "satisfied"),
            ]
        )
        if not task:
            report.progress = ""  # Legacy verifier-only callers have no task relevance map.
        self.runtime["_lastProjectVerification"] = {
            **self.runtime.get("_lastProjectVerification", {}),
            "state": run,
            **({"ok": False, "status": "failed", "reason_code": report.reason_code} if changed else {}),
        }
        return report

    def _verify(self, *, messages, tools, cwd, permissions, project_report=None, acceptance_root=None):
        task = snapshot().get("task")
        ref = (task["id"], task["revision"]) if task else None
        if project_report is not None:
            report = project_report
        elif task and task["kind"] == "answer" and task.get("classification", {}).get("read_only_request"):
            report = VerificationResult(
                False,
                "Explanation-only request; no project programs executed",
                status="not_run",
                reason_code="explanation_only",
            )
        else:
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
        from pepsicode.core.task_control import pending_questions

        if any(q["required"] for q in pending_questions(state, task["id"])):
            return VerificationResult(
                False, "Required user question remains unanswered", status="blocked", reason_code="awaiting_user"
            )
        if task["kind"] == "answer" and (
            (
                task.get("classification", {}).get("read_only_request")
                and task.get("classification", {}).get("source") == "human_instruction"
            )
            or (
                not task["criteria"]
                and not task.get("required_artifacts")
                and not task.get("requirements", {}).get("behavior")
            )
        ):
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
                state["task"]["acceptance"] = None
                state["task"]["evidence"] = []
                if task.get("classification", {}).get("read_only_request"):
                    state["task"]["assurance"] = "explanation_only"
            return report
        if any(
            item.get("task_id") == task["id"] and item.get("required", True) and item["status"] != "completed"
            for item in state.get("todos", [])
        ):
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
        for artifact in task.get("required_artifacts", []):
            path = artifact["path"]
            context = ToolContext(
                str(acceptance_root) if acceptance_root is not None else cwd, permissions=permissions, execution=scope
            )
            if FILES.snapshot(context, path, observe=False).content is None:
                return VerificationResult(
                    False,
                    f"Required artifact is absent: {path}",
                    "Create and inspect the requested artifact; unrelated existing files cannot substitute.",
                    status="failed",
                    reason_code="missing_required_artifact",
                )
        if task.get("criteria_revision") != task["revision"]:
            return VerificationResult(
                False,
                "Review acceptance against the new user instructions",
                "Inspect task_state and define the conditions for this exact revision; earlier evidence cannot accept changed requirements.",
                status="failed",
                reason_code="stale_acceptance",
            )
        if task.get("requirements", {}).get("behavior") and not any(c["kind"] == "command" for c in task["criteria"]):
            return VerificationResult(
                False,
                "Behavior still lacks a targeted check",
                "Add a behavior command; static artifacts cannot prove this structured requirement.",
                status="failed",
                reason_code="behavior_unverified",
            )
        evidence = []
        roots = {}
        for criterion in task["criteria"]:
            item = {"criterion_id": criterion["id"], "task_revision": task["revision"], "status": "unknown"}
            # The delivered version carries the artifact and behavior claims.
            # ``file_unchanged`` protects the user's own workspace and keeps
            # its baseline comparison at the orchestrator's working directory.
            if criterion["kind"] == "file_unchanged" or acceptance_root is None:
                criterion_cwd = cwd
            else:
                criterion_cwd = str(acceptance_root)
            roots[criterion["id"]] = criterion_cwd
            context = ToolContext(
                criterion_cwd,
                permissions=permissions,
                execution=scope,
                cancellation_event=self.project.cancellation_event,
                for_verification=True,
            )
            try:
                if criterion["kind"].startswith("file_"):
                    file = FILES.snapshot(context, criterion["path"], observe=False)
                    passed = file.content is not None
                    if criterion["kind"] == "file_absent":
                        passed = file.content is None
                    if criterion["kind"] == "file_unchanged":
                        baseline = task.get("file_baselines", {}).get(criterion["path"])
                        if baseline is None:
                            raise ValueError("file_unchanged requires a host baseline captured before task execution")
                        passed = file.version == baseline
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
                    context = ToolContext(roots[item["criterion_id"]], permissions=permissions, execution=scope)
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
            latest["status"] = "active"
            latest["assurance"] = (
                "behavior_checked" if any(c["kind"] == "command" for c in task["criteria"]) else "artifact_checked"
            )
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
            "Targeted behavior checks passed; coverage is limited to the declared checks"
            if any(c["kind"] == "command" for c in task["criteria"])
            else "Artifacts checked; behavior has not been verified",
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
        task["recovery"] = {
            "reason_code": last.get("finishReason")
            or (runtime.get("_lastVerification") or {}).get("reason_code")
            or stop_reason,
            "next_action": "observe_and_reconcile"
            if task["status"] == "unknown"
            else "answer_question"
            if task["status"] == "waiting_user"
            else "resume_and_recheck"
            if task["status"] not in {"complete", "answered"}
            else "none",
        }
        runtime["_lastTask"] = json.loads(json.dumps(task))
        for message in reversed(messages):
            if message.get("role") == "assistant":
                message["task"] = runtime["_lastTask"]
                break
