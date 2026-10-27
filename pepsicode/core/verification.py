"""Shared verified execution for terminal, piped CLI and desktop turns."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import asdict
from tempfile import TemporaryDirectory

from pepsicode.core.agent_loop import append_queued_inputs
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.core.loop_engine import VerificationResult, run_loop
from pepsicode.core.session_tree import checkpoint_messages
from pepsicode.core.task_control import begin_task, edit_control, snapshot
from pepsicode.core.task_verification import TaskVerifier, settle_task, settle_task_failure
from pepsicode.core.verification_discovery import Check as Check
from pepsicode.core.verification_discovery import command_timeout, discover_checks
from pepsicode.execution.runtime import scoped_turn
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.run_command import run_command_tool
from pepsicode.workspace import resolve_tool_path


def snapshot_verification_state(tools, runtime=None) -> dict:
    return {
        "version": 1,
        "pending": getattr(tools, "mutation_count", 0) > getattr(tools, "verified_mutation_count", 0),
        "last_report": (runtime or {}).get("_lastVerification"),
    }


def restore_verification_state(tools, runtime, state, messages=()) -> None:
    if isinstance(state, dict) and isinstance(state.get("pending"), bool):
        pending = state["pending"]
    else:
        # Legacy sessions have no verification checkpoint. Recheck previous
        # potentially mutating tool calls once rather than claiming no edits.
        def could_modify_workspace(message):
            if message.get("role") != "assistant_tool_call":
                return False
            tool = tools.find(message.get("toolName", ""))
            if tool is None:
                return True
            try:
                return tool.modifies_workspace(tool.validator(message.get("input", {})))
            except Exception:  # noqa: BLE001 - old inputs may no longer match the tool's schema
                return True

        pending = any(could_modify_workspace(message) for message in messages)
    tools.mutation_count = int(pending)
    tools.verified_mutation_count = 0
    if runtime is not None:
        runtime.pop("_lastVerification", None)
        if isinstance(state, dict) and isinstance(state.get("last_report"), dict):
            runtime["_lastVerification"] = state["last_report"]


def execution_argv(argv: list[str], *, windows: bool) -> list[str]:
    if windows and argv[0].lower() in {"npm", "pnpm", "yarn", "bun"}:
        # Windows package manager shims require cmd. Reject its expansion and
        # control characters instead of silently changing argv semantics.
        if any(re.search(r'["&|<>^%!\r\n]', arg) for arg in argv):
            raise ValueError(
                "Windows package manager arguments contain shell control characters; use a direct executable or wrapper"
            )
        return ["cmd", "/d", "/s", "/c", subprocess.list2cmdline(argv)]
    return argv


class ProjectVerifier:
    def __init__(
        self, runtime: dict, tools, cancellation_event=None, on_tool_start=None, on_tool_result=None, hook_engine=None
    ) -> None:
        self.runtime = runtime
        self.start_count = getattr(tools, "verified_mutation_count", 0)
        self.cancellation_event = cancellation_event if cancellation_event is not None else threading.Event()
        self.on_tool_start = on_tool_start
        self.on_tool_result = on_tool_result
        self.hook_engine = hook_engine

    def verify(self, *, messages, tools, cwd, permissions) -> VerificationResult:
        mode = self.runtime.get("verificationMode", "always" if self.runtime.get("verifierEnabled") else "auto")
        if mode not in {"auto", "always", "off"}:
            return VerificationResult(False, "Invalid verificationMode; choose auto, always or off", status="blocked")
        if mode == "off":
            return VerificationResult(False, "Verification disabled", status="not_run", reason_code="disabled")
        if mode == "auto" and getattr(tools, "mutation_count", 0) <= self.start_count:
            return VerificationResult(
                False,
                "No edits or commands in this turn; verification not run",
                status="not_run",
                reason_code="no_changes",
            )
        if permissions is not None and (permissions.is_plan_mode or permissions.access_mode.value == "read_only"):
            return VerificationResult(
                False, "Read-only/Plan mode; verification commands not run", status="not_run", reason_code="read_only"
            )
        try:
            checks = discover_checks(
                cwd, self.runtime.get("verificationCommands"), self.runtime.get("verificationDiscovery")
            )
            timeout = command_timeout(self.runtime.get("verificationTimeout", 120))
        except (ValueError, OSError, TypeError) as error:
            return VerificationResult(False, str(error), status="blocked")
        if not checks:
            return VerificationResult(
                False, "No verification commands found; result is unverified", status="not_run", reason_code="no_checks"
            )
        reports = []
        failed = []
        not_run = []
        # Verification uses the normal command tool and its permission/cancel
        # path. It does not silently execute package scripts in read-only mode.
        for check in checks:
            if self.cancellation_event is not None and self.cancellation_event.is_set():
                return VerificationResult(False, "Verification stopped by user", status="cancelled")
            context = ToolContext(
                cwd=cwd,
                permissions=permissions,
                cancellation_event=self.cancellation_event,
                hooks=self.hook_engine,
                command_is_argv=True,
                for_verification=True,
            )
            try:
                target = resolve_tool_path(context, check.cwd, "command_cwd")
                argv = execution_argv(check.argv, windows=os.name == "nt")
                data = {"command": argv[0], "args": argv[1:], "cwd": str(target), "timeout": check.timeout or timeout}
                if self.on_tool_start:
                    self.on_tool_start("verification", {"name": check.name, **data})
                # Repeated fixes can keep the same size and filesystem timestamp.
                # Isolate Python bytecode caches so a verification pass cannot
                # accidentally execute a stale .pyc from the previous pass.
                with TemporaryDirectory(prefix="pepsi-verify-cache-") as cache:
                    context.environment = {"PYTHONPYCACHEPREFIX": cache, "PYTHONDONTWRITEBYTECODE": "1", **check.env}
                    command_tools = tools if tools.find("run_command") is not None else ToolRegistry([run_command_tool])
                    result = command_tools.execute("run_command", data, context)
                if self.on_tool_result:
                    self.on_tool_result("verification", f"{check.name}\n{result.output}", not result.ok)
            except (ValueError, OSError, PermissionError) as error:
                return VerificationResult(False, f"Verification could not run: {check.name}", str(error), "blocked")
            if self.cancellation_event is not None and self.cancellation_event.is_set():
                return VerificationResult(False, "Verification stopped by user", result.output, "cancelled")
            output = result.output
            outcome = (
                "not_run" if result.exit_code in check.no_tests_exit_codes else "passed" if result.ok else "failed"
            )
            reports.append(f"{check.name}: {outcome}\n{output[:8000]}")
            if result.status in {"blocked", "cancelled", "error"}:
                return VerificationResult(
                    False,
                    f"Verification could not run: {check.name}",
                    "\n".join(reports),
                    "cancelled" if result.status == "cancelled" else "blocked",
                )
            if outcome == "not_run":
                not_run.append(check.name)
            elif not result.ok:
                failed.append(check.name)
        if failed:
            return VerificationResult(
                False, f"{len(failed)} verification check(s) failed: {', '.join(failed)}", "\n".join(reports), "failed"
            )
        if not_run:
            return VerificationResult(
                False,
                f"No tests collected: {', '.join(not_run)}",
                "\n".join(reports),
                "not_run",
                reason_code="no_tests",
            )
        tools.verified_mutation_count = getattr(tools, "mutation_count", 0)
        return VerificationResult(True, f"{len(checks)} verification check(s) passed", "\n".join(reports), "passed")


@scoped_turn
def run_verified_turn(
    *,
    runtime: dict | None,
    runner,
    input_queue: TurnInputQueue | None = None,
    on_verification=None,
    task_kind=None,
    acceptance=None,
    task_requirements=None,
    **options,
):
    """Finish verification before admitting the next queued task."""
    runtime = runtime if runtime is not None else {}
    if input_queue is None:
        return _run_verified_turn_once(
            runtime=runtime,
            runner=runner,
            on_verification=on_verification,
            task_kind=task_kind,
            acceptance=acceptance,
            task_requirements=task_requirements,
            **options,
        )
    try:
        while True:
            messages = _run_verified_turn_once(
                runtime=runtime,
                runner=runner,
                on_verification=on_verification,
                input_queue=input_queue,
                task_kind=task_kind,
                acceptance=acceptance,
                task_requirements=task_requirements,
                **options,
            )
            event = options.get("cancellation_event")
            report = runtime.get("_lastVerification") or {}
            if (
                (event is not None and event.is_set())
                or (messages and (messages[-1].get("isError") or messages[-1].get("awaitingUser")))
                or report.get("status") in {"failed", "blocked", "cancelled"}
            ):
                return messages
            pending = input_queue.next_or_close()
            if not pending:
                return messages
            append_queued_inputs(
                messages,
                pending,
                cwd=options["cwd"],
                permissions=options.get("permissions"),
                hook_engine=options.get("hook_engine"),
                agent_scope=options.get("agent_scope", "main"),
            )
            options["messages"] = messages
            task_kind, acceptance = None, None
            task_requirements = None
    finally:
        input_queue.close()


@settle_task_failure
def _run_verified_turn_once(
    *,
    runtime: dict | None,
    runner,
    on_verification=None,
    task_kind=None,
    acceptance=None,
    task_requirements=None,
    **options,
):
    """Run the existing agent with optional streaming and a bounded verifier."""
    runtime = runtime if runtime is not None else {}
    contract_options = {**runtime, **({"taskKind": task_kind} if task_kind is not None else {})}
    task = begin_task(options.get("messages", []), contract_options)
    if task:
        from pepsicode.core.task_acceptance import validate_criteria

        inputs = runtime.get("verificationInputs", [])
        validate_criteria([{"id": str(i), "kind": "file_exists", "path": path} for i, path in enumerate(inputs)])
        with edit_control() as control:
            control["task"]["verification_inputs"] = inputs
    if task and task_requirements is not None:
        from pepsicode.core.task_acceptance import validate_requirements

        requirements = validate_requirements(task_requirements)
        with edit_control() as control:
            control["task"]["requirements"] = requirements
            control["task"]["required_artifacts"] = [
                {"path": path, "source": requirements["source"]} for path in requirements.get("artifacts", [])
            ]
            if requirements["artifacts"] and not control["task"]["criteria"]:
                control["task"]["criteria"] = [
                    {"id": f"required-{i}", "kind": "file_exists", "path": path}
                    for i, path in enumerate(requirements["artifacts"])
                ]
                control["task"]["criteria_revision"] = control["task"]["revision"]
            for condition in control["task"]["criteria"]:
                if condition.get("path") in requirements["artifacts"]:
                    control["task"].setdefault("criterion_sources", {})[condition["id"]] = {
                        "source": requirements["source"],
                        "input_id": task["inputs"][-1],
                        "purpose": "Verify an explicitly required artifact",
                    }
            if requirements["behavior"] or requirements["artifacts"]:
                control["task"]["classification"] = {"source": "caller", "locked": True, "kind": "change"}
            if (requirements.get("behavior") or requirements["artifacts"]) and control["task"]["kind"] == "answer":
                control["task"]["kind"] = "change"
    configured = acceptance if acceptance is not None else runtime.get("taskAcceptance")
    if task and configured is not None:
        from pepsicode.core.task_acceptance import validate_criteria

        with edit_control() as control:
            control["task"]["criteria"] = validate_criteria(configured)
            control["task"]["criteria_revision"] = control["task"]["revision"]
            control["task"]["criterion_sources"] = {
                c["id"]: {"source": "caller", "input_id": task["inputs"][-1]} for c in control["task"]["criteria"]
            }
    if task:
        from pepsicode.execution.filesystem import FILES
        from pepsicode.tooling import ToolContext

        current = snapshot()["task"]
        paths = {item["path"] for item in current.get("file_mentions", [])}
        paths.update(c["path"] for c in current["criteria"] if c["kind"] == "file_unchanged")
        context = ToolContext(options["cwd"], permissions=options.get("permissions"))
        baselines = {
            path: FILES.snapshot(context, path, observe=False).version
            for path in paths
            if path not in current.get("file_baselines", {})
        }
        with edit_control() as control:
            control["task"].setdefault("file_baselines", {}).update(baselines)
    runtime.pop("_lastVerification", None)
    event = options.get("cancellation_event")
    report = None
    progress = options.get("on_progress_message")

    def verified(result, announce=True):
        nonlocal report
        report = asdict(result)
        report["status"] = result.status or ("passed" if result.ok else "failed")
        runtime["_lastVerification"] = report
        if on_verification:
            on_verification(report)
        if announce and progress and not on_verification and report["status"] != "not_run":
            progress(f"Verification [{report['status']}]: {result.reason}")

    verifier = ProjectVerifier(
        runtime,
        options["tools"],
        event,
        options.get("on_tool_start"),
        options.get("on_tool_result"),
        options.get("hook_engine"),
    )
    common_keys = {"model", "tools", "messages", "cwd", "permissions", "context_manager", "cost_tracker", "memory"}
    common = {key: options[key] for key in common_keys if key in options}
    turn_options = {
        key: value
        for key, value in options.items()
        if key not in common_keys | {"cancellation_event", "cost_limit_usd"}
    }
    # Hooks belong to every turn, including retries.
    result = run_loop(
        **common,
        runner=runner,
        turn_options=turn_options,
        cancellation_event=event,
        verifier=TaskVerifier(verifier, runtime),
        max_iterations=runtime.get("maxIterations", 3),
        no_progress_limit=runtime.get("verificationNoProgressLimit", 2),
        cost_limit_usd=options.get("cost_limit_usd", runtime.get("budgetLimitUsd")),
        on_verify=verified,
        model_recovery_limit=runtime.get("modelRecoveryLimit", 1),
    )
    if report is None:
        verified(
            VerificationResult(
                False,
                "Verification stopped by user"
                if result.stop_reason == "cancelled"
                else "Agent turn failed before verification",
                status="cancelled" if result.stop_reason == "cancelled" else "not_run",
            ),
            announce=False,
        )
    if result.stop_reason in {"no_progress", "max_iterations"}:
        message = f"Verification failed; stopped after {result.iterations} iteration(s) ({result.stop_reason}). {report['reason'] if report else ''}"
        result.messages.append({"role": "assistant", "content": message, "isError": True})
        callback = options.get("on_error_message") or options.get("on_assistant_message")
        if callback:
            callback(message)
    if (
        report
        and report.get("reason_code") == "awaiting_user"
        and not (result.messages and result.messages[-1].get("awaitingUser"))
    ):
        pending = [
            q["question"]
            for q in snapshot().get("questions", [])
            if q["status"] == "pending" and q.get("required", True)
        ]
        result.messages.append(
            {"role": "assistant", "content": "\n".join(pending) or "Waiting for your reply.", "awaitingUser": True}
        )
    checkpoint_messages(result.messages)
    settle_task(result.messages, runtime, result.stop_reason)
    permissions = options.get("permissions")
    if permissions and hasattr(permissions, "clear_task_grants"):
        permissions.clear_task_grants()
    if report is not None:
        for message in reversed(result.messages):
            if message.get("role") == "assistant":
                message["verification"] = report
                break
    return result.messages
