"""Bind queued attempts to the existing child Agent and execution harness."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Timer
from typing import Any

from pepsicode.cancellation import TurnCancelledError
from pepsicode.core.session_tree import checkpoint_execution, recording_session
from pepsicode.execution.runtime import (
    ExecutionScope,
    bind_scope,
    execution_journal_dir,
    recover_execution_calls,
)
from pepsicode.hooks.engine import HookEngine
from pepsicode.llm.cost_tracker import BudgetExceededError
from pepsicode.orchestration.budget import bind_budget
from pepsicode.orchestration.process_worker import create_process_runner
from pepsicode.orchestration.repair import classify_failure
from pepsicode.permissions import PermissionMode
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.task import create_task_tool

# Worker report statuses that settle the attempt as a non-success outcome.
# ``dropped`` is a definite end of the assignment, not a wait.
_REPORT_FAILURES = {"failed", "blocked", "dropped"}
_SUCCEEDED_TOOL = "completed"
_EVIDENCE_OUTPUT_LIMIT = 800


def _trace_node(session, trace_id):
    manager = getattr(session, "agent_traces", None)
    return manager.get(trace_id) if manager is not None else None


def _final_report(session, trace_id):
    """The Worker's last structured report, unless it was archived as stale."""
    node = _trace_node(session, trace_id)
    if node is None or not node.reports:
        return None
    latest = node.reports[-1]
    return None if latest.stale else latest


def _failure_evidence(session, item, scope, stage, report_summary, output):
    """Bounded structured evidence for the coordinator, from host-observed facts.

    The JSON prefix carries the settlement stage, a failure class derived from
    host tool outcomes (never the Worker's self-assessment alone), the failing
    check name, an output excerpt and the execution journal reference. Trace
    IDs and timing stay out of it so repeated identical failures keep a stable
    repair fingerprint.
    """
    node = _trace_node(session, item["trace_id"])
    failed = (
        next((preview for preview in reversed(node.recent_tools or ()) if preview.status != _SUCCEEDED_TOOL), None)
        if node is not None
        else None
    )
    observed = " ".join(part for part in (failed.output if failed is not None else "", report_summary or "") if part)
    evidence = {
        "stage": stage,
        "class": classify_failure({"summary": observed}),
        "check": failed.tool if failed is not None else None,
        "output": (report_summary or output or "")[:_EVIDENCE_OUTPUT_LIMIT],
        "journal": str(scope.journal.path) if scope.journal is not None else None,
    }
    prefix = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}\n{output}"[:4000]


@dataclass
class WorkerExecutor:
    scope: ExecutionScope
    tools: ToolRegistry
    runtime: dict | None = None
    model_factory: Callable[[ToolRegistry], Any] | None = None
    hooks: HookEngine | None = None
    prepared_process: bool = False
    isolation: str = "shared"
    coordinator_reserve: int = 2

    def is_read_only(self, item):
        # Explore is forced through the read-only allowlist by the Task tool.
        return item["specification"].get("agent_type") == "explore" and not (
            self.hooks and self.hooks.has_command_actions()
        )

    def attribute_output(self, session, item, scope):
        """Record one settled attempt's committed output in its managed manifest.

        The Worker commits on the managed branch inside its own worktree. When
        the worktree head continues linearly from the recorded baseline, the
        commits are attributable to this work item and the manifest keeps the
        bounded provenance; anything else stays unrecorded and the next
        launch validation reports the drift for review.
        """
        if not self.prepared_process or item["specification"].get("agent_type") != "general":
            return None
        try:
            from pepsicode.orchestration.environment import attribute_managed_output

            return attribute_managed_output(session, item, scope)
        except (OSError, RuntimeError, ValueError):
            return None

    @contextmanager
    def bind(self, session, *, generation):
        scope = self.scope
        if os.path.normcase(str(Path(scope.cwd).resolve())) != os.path.normcase(str(Path(session.workspace).resolve())):
            raise ValueError("Worker scope does not match the session workspace")
        if (
            scope.parent_task_id is not None
            or scope.budget is None
            or scope.journal is None
            or scope.cancellation_event is None
            or scope.permissions is None
        ):
            raise ValueError("Managed Workers require a root scope with budget, journal, cancellation and permissions")
        if self.runtime is None and self.model_factory is None:
            raise ValueError("Managed Workers require a configured model")
        path = scope.journal.path.resolve()
        if path.parent != execution_journal_dir(scope.cwd) or path.suffix != ".jsonl":
            raise ValueError("Worker journal must belong to the workspace's execution directory")
        if scope.has_unknown or recover_execution_calls(scope.cwd, session.execution_state):
            raise PermissionError("Reconcile the previous execution before starting Workers")
        if (session.execution_state or {}).get("usage_uncertain"):
            scope.budget.unmetered = True

        def execute(item, stop):
            if stop.is_set() or scope.cancellation_event.is_set():
                return "cancelled", "The Worker was stopped before execution."
            if scope.permissions.is_plan_mode or scope.permissions.mode == PermissionMode.READ_ONLY:
                return "blocked", "The execution mode no longer permits managed dispatch."
            if scope.has_unknown:
                raise RuntimeError("Worker launch is blocked by an unknown execution outcome")
            try:
                if scope.policy_checkpoint:
                    scope.policy_checkpoint()
                scope.budget.check_worker()
            except (BudgetExceededError, RuntimeError, PermissionError) as error:
                return "blocked", str(error)
            specification = item["specification"]
            if specification.get("agent_type") not in {"explore", "general"}:
                return "blocked", "Unsupported managed Worker role."
            cwd = session.workspace
            worker_runner = None
            worker_tools = self.tools
            hooks = self.hooks
            if self.prepared_process and specification["agent_type"] == "general":
                from pepsicode.orchestration.environment import validate_prepared_environment

                try:
                    with recording_session(session), bind_scope(scope):
                        validation = scope.child(cancellation_event=stop)
                        validation.trace_id = item["trace_id"]
                        with bind_scope(validation):
                            cwd, directories = validate_prepared_environment(session, item, validation)
                    environment = session.task_control["orchestration"]["environments"][item["work_id"]]
                    sandbox = None
                    if environment["isolation_effective"] == "hardened":
                        from pepsicode.sandbox.bubblewrap import BubblewrapBackend

                        sandbox = BubblewrapBackend()
                        hooks = None
                        worker_tools = ToolRegistry(
                            [
                                tool
                                for tool in self.tools.list()
                                if tool.name
                                in {
                                    "read_file",
                                    "list_files",
                                    "grep_files",
                                    "write_file",
                                    "edit_file",
                                    "multi_edit",
                                    "patch_file",
                                    "run_command",
                                    "test_runner",
                                    "agent_report",
                                }
                            ]
                        )
                except TurnCancelledError:
                    return "cancelled", "The Worker was stopped during checkout validation."
                except (KeyError, OSError, RuntimeError, ValueError) as error:
                    return "blocked", f"Worker environment unavailable: {error}"
                worker_runner = create_process_runner(
                    run_id=item["run_id"],
                    attempt_id=item["attempt_id"],
                    generation=generation,
                    directories=directories,
                    sandbox=sandbox,
                    port_range=environment.get("port_range"),
                )
            tool = create_task_tool(
                session.workspace,
                self.runtime,
                model_factory=self.model_factory,
                attempt=item,
                worker_runner=worker_runner,
            )
            with recording_session(session), bind_scope(scope):
                result = tool.run(
                    tool.validator({"agent_type": specification["agent_type"], "task": specification["goal"]}),
                    ToolContext(
                        cwd,
                        permissions=scope.permissions,
                        cancellation_event=stop,
                        hooks=hooks,
                        registry=worker_tools,
                        tool_use_id=item["attempt_id"],
                    ),
                )
                self.attribute_output(session, item, scope)
            if self.hooks and self.hooks.has_command_actions():
                self.hooks.wait_background()
            status = (result.details or {}).get("status") or result.status
            if status == "unknown" or scope.has_unknown:
                raise RuntimeError("Worker has an unknown execution outcome; inspect its journal")
            if result.awaitUser:
                return "waiting_user", result.output
            if result.ok:
                # A finished model reply is not the work result: the Worker's
                # final structured report settles the attempt truthfully, so
                # an explicit failed/blocked report reaches the coordinator as
                # that outcome instead of a result to verify.
                report = _final_report(session, item["trace_id"])
                if report is not None and report.status in _REPORT_FAILURES:
                    return "blocked" if report.status == "blocked" else "failed", _failure_evidence(
                        session, item, scope, report.status, report.summary, result.output
                    )
                return "result_ready", result.output
            if status in {"cancelled", "timed_out"}:
                return "cancelled", result.output
            return ("blocked" if result.status == "blocked" else "failed"), result.output

        with recording_session(session), bind_scope(scope):
            bind_budget(session, scope)
            scope.budget.coordinator_reserve = self.coordinator_reserve
            checkpoint_execution(scope.snapshot())
            timer = None
            if scope.deadline is not None:
                remaining = scope.deadline - time.monotonic()
                if remaining <= 0:
                    scope.cancellation_event.set()
                else:
                    timer = Timer(remaining, scope.cancellation_event.set)
                    timer.daemon = True
                    timer.start()
            try:
                yield execute
            finally:
                if timer is not None:
                    timer.cancel()
                checkpoint_execution(scope.snapshot())
