"""Tools bound to a single orchestration host callback."""

from __future__ import annotations

import json
from contextlib import contextmanager
from functools import partial
from threading import get_ident

from pepsicode.core.session_tree import current_session, recording_session
from pepsicode.execution.runtime import current_scope
from pepsicode.llm.cost_tracker import BudgetExceededError
from pepsicode.orchestration.git_ops import WorktreeBaselineError, check_worktree_baseline
from pepsicode.orchestration.reporting import worker_report_only
from pepsicode.orchestration.store import admit_work, note_work, retry_work
from pepsicode.permissions import PermissionMode
from pepsicode.tooling import ToolCapability, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.agent_status import create_agent_status


def _validate(data, *, require_deliverable=False):
    if not isinstance(data, dict) or set(data) - {"deliverable"} != {"work_key", "goal", "agent_type"}:
        raise ValueError("Specify work_key, goal and agent_type")
    key, goal, agent_type = data["work_key"], data["goal"], data["agent_type"]
    if not isinstance(key, str) or not 0 < len(key.strip()) <= 128:
        raise ValueError("work_key must contain 1–128 characters")
    if not isinstance(goal, str) or not 0 < len(goal.strip()) <= 4000:
        raise ValueError("goal must contain 1–4000 characters")
    if agent_type not in ("explore", "general"):
        raise ValueError("agent_type must be explore or general")
    specification = {"goal": goal.strip(), "agent_type": agent_type}
    if require_deliverable or "deliverable" in data:
        if data.get("deliverable") not in ("project", "worker_reports"):
            raise ValueError("Choose the parent deliverable: project or worker_reports")
        specification["deliverable"] = data["deliverable"]
    return key.strip(), specification


@contextmanager
def coordinator_turn(
    session, *, cancel=None, admitted_only=False, require_deliverable=False, runtime=None, workspace_owned=False
):
    """Expose dispatch and status while the scheduler owns this callback."""
    owner_thread = get_ident()
    task = session.task_control.get("task") or {}
    task_id = task.get("id")
    settings = runtime if runtime is not None else getattr(session, "runtime", None) or {}
    orchestration_settings = settings.get("orchestration") or {}
    queue_limit = orchestration_settings.get("maxQueuedWorkItems", 32)
    active_limit = orchestration_settings.get("maxActiveWorkItemsPerRun", 32)
    max_repair_attempts = orchestration_settings.get("maxRepairAttempts", 2)
    max_auto_retries = orchestration_settings.get("maxAutoRetries", 1)
    require_git_baseline = orchestration_settings.get("workerIsolation", "shared") == "hardened"
    active = True

    def current_revision():
        task = session.task_control.get("task") or {}
        if admitted_only:
            return task.get("admitted_revision", task.get("revision"))
        return task.get("revision")

    def authorize(context):
        if not active or get_ident() != owner_thread or current_session() is not session:
            return ToolResult(False, "No active orchestration turn.", status="blocked")
        permissions = context.permissions
        if context.agent_scope != "main" or (
            permissions and (permissions.is_plan_mode or permissions.mode == PermissionMode.READ_ONLY)
        ):
            return ToolResult(False, "Dispatch is disabled in this execution mode.", status="blocked")
        if current_scope().has_unknown:
            return ToolResult(False, "Reconcile the unknown operation before dispatch.", status="blocked")

    def dispatch(arguments, context):
        if denied := authorize(context):
            return denied
        work_key, specification = arguments
        general = specification["agent_type"] == "general"
        task = session.task_control.get("task") or {}
        if general and task.get("classification", {}).get("read_only_request"):
            return ToolResult(False, "Only investigation can be dispatched for a read-only request.", status="blocked")
        revision = current_revision()
        try:
            scope = current_scope()
            budget = scope.budget
            if budget is not None:
                budget.check_worker()
            if general and require_git_baseline:
                check_worktree_baseline(context, session.workspace)
            item = admit_work(
                session,
                task_id=task_id,
                revision=revision,
                work_key=work_key,
                specification=specification,
                limit=queue_limit,
                active_limit=active_limit,
                receipt_key=f"{scope.task_id}:{context.tool_use_id}" if scope.journal and context.tool_use_id else None,
            )
        except WorktreeBaselineError as error:
            return ToolResult(
                False, str(error), status="blocked", awaitUser=specification.get("deliverable") == "worker_reports"
            )
        except (ValueError, PermissionError, BudgetExceededError, RuntimeError) as error:
            return ToolResult(False, str(error), status="blocked")
        except OSError:
            return ToolResult(False, "Dispatch could not be saved.", status="failed")
        receipt = {
            "accepted": True,
            "deliverable": session.task_control["task"]["orchestration_output"]["kind"],
            **{key: item[key] for key in ("run_id", "work_id", "attempt_id", "trace_id", "state", "revision")},
        }
        return ToolResult(True, json.dumps(receipt), details=receipt)

    tool = ToolDefinition(
        name="task_dispatch",
        description="Queue a scoped Worker assignment and return its durable identity immediately.",
        input_schema={
            "type": "object",
            "properties": {
                "work_key": {"type": "string", "maxLength": 128},
                "goal": {"type": "string", "maxLength": 4000},
                "agent_type": {"enum": ["explore", "general"]},
                "deliverable": {
                    "type": "string",
                    "enum": ["project", "worker_reports"],
                    "description": "Final outcome requested by the user for the whole task. Choose project for a usable implementation in the project, even when a summary is also requested. Choose worker_reports when the user only wants findings or asks to leave examples in the Workers' own workspaces and review the results.",
                },
            },
            "required": ["work_key", "goal", "agent_type"] + (["deliverable"] if require_deliverable else []),
            "additionalProperties": False,
        },
        validator=partial(_validate, require_deliverable=require_deliverable),
        run=dispatch,
        capabilities={ToolCapability.SESSION_METADATA},
        affects_workspace=False,
        always_loaded=True,
    )
    tools = [tool, create_agent_status(session.agent_traces)]
    if cancel is not None:
        attempt_field = {"type": "string", "maxLength": 64}

        def validate_attempt(data):
            attempt_id = data.get("attempt_id") if isinstance(data, dict) else None
            if not isinstance(attempt_id, str) or not 0 < len(attempt_id) <= 64:
                raise ValueError("Specify a valid attempt_id")
            return attempt_id

        def stop(attempt_id, context):
            if denied := authorize(context):
                return denied
            try:
                state = cancel(task_id, current_revision(), attempt_id)
            except ValueError as error:
                return ToolResult(False, str(error), status="blocked")
            except OSError:
                return ToolResult(False, "Stop request could not be saved.", status="failed")
            receipt = {"attempt_id": attempt_id, "state": state}
            return ToolResult(True, json.dumps(receipt), details=receipt)

        tools.append(
            ToolDefinition(
                name="task_cancel",
                description="Request cancellation of one current Worker attempt by attempt_id.",
                input_schema={
                    "type": "object",
                    "properties": {"attempt_id": {"type": "string", "maxLength": 64}},
                    "required": ["attempt_id"],
                },
                validator=validate_attempt,
                run=stop,
                capabilities={ToolCapability.SESSION_METADATA},
                affects_workspace=False,
                always_loaded=True,
            )
        )

        def retry(attempt_id, context):
            if denied := authorize(context):
                return denied
            revision = current_revision()
            try:
                current_scope().budget.check_worker()
                if require_git_baseline:
                    state = session.task_control.get("orchestration") or {}
                    attempt = next((row for row in state.get("work", []) if row["attempt_id"] == attempt_id), None)
                    if (
                        attempt
                        and attempt["specification"].get("agent_type") == "general"
                        and attempt["work_id"] not in state.get("environments", {})
                    ):
                        check_worktree_baseline(context, session.workspace)
                item = retry_work(
                    session,
                    task_id=task_id,
                    revision=revision,
                    attempt_id=attempt_id,
                    max_repair_attempts=max_repair_attempts,
                    max_auto_retries=max_auto_retries,
                    queue_limit=queue_limit,
                    active_limit=active_limit,
                )
            except WorktreeBaselineError as error:
                return ToolResult(
                    False, str(error), status="blocked", awaitUser=worker_report_only(session.task_control["task"])
                )
            except (ValueError, PermissionError, BudgetExceededError, RuntimeError) as error:
                return ToolResult(False, str(error), status="blocked")
            except OSError:
                return ToolResult(False, "Retry could not be saved.", status="failed")
            receipt = {key: item[key] for key in ("run_id", "work_id", "attempt_id", "trace_id", "state", "revision")}
            return ToolResult(True, json.dumps(receipt), details=receipt)

        def validate_note(data):
            attempt_id = validate_attempt(data)
            note = data.get("note")
            if not isinstance(note, str) or not 0 < len(note.strip()) <= 800:
                raise ValueError("note must contain 1–800 characters")
            return attempt_id, note.strip()

        def note(arguments, context):
            if denied := authorize(context):
                return denied
            attempt_id, content = arguments
            try:
                count = note_work(
                    session, task_id=task_id, revision=current_revision(), attempt_id=attempt_id, note=content
                )
            except ValueError as error:
                return ToolResult(False, str(error), status="blocked")
            except OSError:
                return ToolResult(False, "Context note could not be saved.", status="failed")
            return ToolResult(True, f"Context saved. {count} note(s) queued.")

        def request_report(arguments, context):
            if denied := authorize(context):
                return denied
            attempt_id, question = arguments
            revision = current_revision()
            state = session.task_control.get("orchestration") or {}
            original = next(
                (
                    row
                    for row in state.get("work", [])
                    if row["attempt_id"] == attempt_id
                    and (row["task_id"], row["revision"], row["state"]) == (task_id, revision, "result_ready")
                ),
                None,
            )
            if original is None:
                return ToolResult(
                    False, "Only a current, completed Worker result can be followed up.", status="blocked"
                )
            return dispatch(
                (
                    f"report:{attempt_id}",
                    {
                        "agent_type": "explore",
                        "goal": (
                            f"Clarify this Worker result without making changes.\n"
                            f"Original goal: {original['specification'].get('goal', '')[:800]}\n"
                            f"Result: {original['summary'][:1400]}\nQuestion: {question}"
                        ),
                    },
                ),
                context,
            )

        tools.extend(
            [
                ToolDefinition(
                    name="task_retry",
                    description="Queue a new attempt for an eligible failed Worker or an answered question.",
                    input_schema={
                        "type": "object",
                        "properties": {"attempt_id": attempt_field},
                        "required": ["attempt_id"],
                    },
                    validator=validate_attempt,
                    run=retry,
                    capabilities={ToolCapability.SESSION_METADATA},
                    affects_workspace=False,
                    always_loaded=True,
                ),
                ToolDefinition(
                    name="task_note",
                    description="Add context to a queued attempt without changing its assignment or authorization.",
                    input_schema={
                        "type": "object",
                        "properties": {"attempt_id": attempt_field, "note": {"type": "string", "maxLength": 800}},
                        "required": ["attempt_id", "note"],
                    },
                    validator=validate_note,
                    run=note,
                    capabilities={ToolCapability.SESSION_METADATA},
                    affects_workspace=False,
                    always_loaded=True,
                ),
                ToolDefinition(
                    name="task_request_report",
                    description="Request one read-only clarification of a completed Worker result. Uses the current budget.",
                    input_schema={
                        "type": "object",
                        "properties": {"attempt_id": attempt_field, "note": {"type": "string", "maxLength": 800}},
                        "required": ["attempt_id", "note"],
                    },
                    validator=validate_note,
                    run=request_report,
                    capabilities={ToolCapability.SESSION_METADATA},
                    affects_workspace=False,
                    always_loaded=True,
                ),
            ]
        )

    def validate_work_ids(data):
        work_ids = data.get("work_ids") if isinstance(data, dict) else None
        if (
            not isinstance(work_ids, list)
            or not 1 <= len(work_ids) <= 8
            or any(not isinstance(item, str) or not 0 < len(item) <= 64 for item in work_ids)
        ):
            raise ValueError("work_ids must contain 1–8 work item IDs")
        return work_ids

    def integrate(work_ids, context):
        if denied := authorize(context):
            return denied
        # Imported here: orchestration.runtime imports this module.
        from pepsicode.orchestration.integration import integrate_candidates, run_integration_acceptance

        try:
            record = integrate_candidates(session, current_scope(), work_ids, workspace_owned=workspace_owned)
            if record["state"] == "integrated":
                record = run_integration_acceptance(
                    session, current_scope(), record["integration_id"], runtime=settings
                )
        except (ValueError, PermissionError, RuntimeError) as error:
            return ToolResult(False, str(error), status="blocked")
        except OSError:
            return ToolResult(False, "Integration could not be saved.", status="failed")
        receipt = {
            "integration_id": record["integration_id"],
            "state": record["state"],
            "candidates": record["candidates"],
            "conflicts": record["conflicts"],
            "head_sha": record.get("head_sha", ""),
            "acceptance": record.get("acceptance"),
        }
        return ToolResult(True, json.dumps(receipt), details=receipt)

    def validate_deliver(data):
        if not isinstance(data, dict):
            raise ValueError("Specify an integration_id")
        integration_id = data.get("integration_id")
        if not isinstance(integration_id, str) or not 0 < len(integration_id) <= 64:
            raise ValueError("Specify a valid integration_id")
        for field in ("push", "pull_request"):
            if field in data and not isinstance(data[field], bool):
                raise ValueError(f"{field} must be a boolean")
        for field in ("repository", "base_branch"):
            if field in data and (not isinstance(data[field], str) or len(data[field]) > 200):
                raise ValueError(f"{field} must be a string")
        return (
            integration_id,
            bool(data.get("push")),
            bool(data.get("pull_request")),
            data.get("repository"),
            data.get("base_branch"),
        )

    def deliver(arguments, context):
        if denied := authorize(context):
            return denied
        # Imported here: orchestration.runtime imports this module.
        from pepsicode.orchestration.external_delivery import deliver_integration

        integration_id, push, pull_request, repository, base_branch = arguments
        try:
            record = deliver_integration(
                session,
                current_scope(),
                integration_id,
                push=push,
                pull_request=pull_request,
                repository=repository,
                base_branch=base_branch,
                runtime=settings,
            )
        except (ValueError, PermissionError, RuntimeError) as error:
            return ToolResult(False, str(error), status="blocked")
        except OSError:
            return ToolResult(False, "Delivery record could not be saved.", status="failed")
        receipt = {
            key: record[key]
            for key in ("delivery_id", "integration_id", "kind", "status", "head_sha", "branch", "external_refs")
        }
        return ToolResult(True, json.dumps(receipt), details=receipt)

    tools.extend(
        [
            ToolDefinition(
                name="task_integrate",
                description=(
                    "Integrate the committed output of delivered Workers in a dedicated worktree, "
                    "then run the configured acceptance commands bound to the integration HEAD."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"work_ids": {"type": "array", "items": {"type": "string", "maxLength": 64}}},
                    "required": ["work_ids"],
                    "additionalProperties": False,
                },
                validator=validate_work_ids,
                run=integrate,
                capabilities={ToolCapability.SESSION_METADATA},
                affects_workspace=False,
                always_loaded=True,
            ),
            ToolDefinition(
                name="task_deliver",
                description=(
                    "Deliver an accepted integration: local candidate record by default; "
                    "push=True pushes to the remote (needs execution approval or policy); "
                    "pull_request=True opens a PR for the integration branch."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "integration_id": {"type": "string", "maxLength": 64},
                        "push": {"type": "boolean"},
                        "pull_request": {"type": "boolean"},
                        "repository": {"type": "string", "maxLength": 200},
                        "base_branch": {"type": "string", "maxLength": 200},
                    },
                    "required": ["integration_id"],
                    "additionalProperties": False,
                },
                validator=validate_deliver,
                run=deliver,
                capabilities={ToolCapability.SESSION_METADATA},
                affects_workspace=False,
                always_loaded=True,
            ),
        ]
    )
    with recording_session(session):
        try:
            yield ToolRegistry(tools)
        finally:
            active = False
