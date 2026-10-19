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
from pepsicode.core.verification_discovery import Check as Check
from pepsicode.core.verification_discovery import command_timeout, discover_checks
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
            return VerificationResult(False, "Read-only/Plan mode; verification commands not run", status="not_run")
        try:
            checks = discover_checks(
                cwd, self.runtime.get("verificationCommands"), self.runtime.get("verificationDiscovery")
            )
            timeout = command_timeout(self.runtime.get("verificationTimeout", 120))
        except (ValueError, OSError, TypeError) as error:
            return VerificationResult(False, str(error), status="blocked")
        if not checks:
            return VerificationResult(False, "No verification commands found; result is unverified", status="not_run")
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
            )
            try:
                target = resolve_tool_path(context, check.cwd, "command_cwd")
                if permissions is not None:
                    outcome = permissions.check_command(
                        check.argv[0],
                        check.argv[1:],
                        str(target),
                        force_prompt_reason="Run project verification scripts, which execute repository code",
                    )
                    if outcome.is_denied or outcome.is_unavailable:
                        return VerificationResult(
                            False,
                            f"Verification requires approval: {check.name}",
                            outcome.denial_message(scope=check.name),
                            "blocked",
                        )
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


def run_verified_turn(
    *, runtime: dict | None, runner, input_queue: TurnInputQueue | None = None, on_verification=None, **options
):
    """Finish verification before admitting the next queued task."""
    runtime = runtime if runtime is not None else {}
    if input_queue is None:
        return _run_verified_turn_once(runtime=runtime, runner=runner, on_verification=on_verification, **options)
    try:
        while True:
            messages = _run_verified_turn_once(
                runtime=runtime, runner=runner, on_verification=on_verification, input_queue=input_queue, **options
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
    finally:
        input_queue.close()


def _run_verified_turn_once(*, runtime: dict | None, runner, on_verification=None, **options):
    """Run the existing agent with optional streaming and a bounded verifier."""
    runtime = runtime if runtime is not None else {}
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
        if announce and progress and result.reason_code not in {"no_changes", "awaiting_user"}:
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
        verifier=verifier,
        max_iterations=runtime.get("maxIterations", 3),
        no_progress_limit=runtime.get("verificationNoProgressLimit", 2),
        cost_limit_usd=options.get("cost_limit_usd", runtime.get("budgetLimitUsd")),
        on_verify=verified,
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
    return result.messages
