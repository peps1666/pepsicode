"""Permission manager — Plan-mode boundary + approval/sandbox composition.

Responsibilities retained here:
  * Plan mode state machine (enter/restore/approve/exit).
  * Plan-mode tool & local-command gating (non-bypassable capability boundary).
  * Composition of :class:`ApprovalBackend`, :class:`SandboxBackend`, and
    :class:`ApprovalStore` into a single facade used by tools and the agent
    loop.

Approval policy state (allow_always / deny_always caches) has been extracted
to :mod:`pepsicode.approval.store`.  Dangerous-command classification lives
in :mod:`pepsicode.sandbox.classifier`.  This module is now a thin orchestrator.

Backward compatibility:
  * ``ensure_command`` / ``ensure_edit`` / ``ensure_path_access`` are kept as
    thin wrappers that call the new ``check_*`` methods and raise
    ``RuntimeError`` on denial.  Existing callers continue to work; new code
    should prefer the ``check_*`` methods which return ``ApprovalOutcome``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from typing import Any

from pepsicode.approval import (
    ApprovalBackend,
    ApprovalDecision,
    ApprovalKind,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStore,
    LocalApprovalBackend,
)
from pepsicode.approval.local_backend import PromptHandler
from pepsicode.sandbox import (
    LocalSandboxBackend,
    SandboxBackend,
    classify_dangerous_command,
    format_command_signature,
)

# Kept for backward compatibility — many modules import these names from here.
__all__ = [
    "PermissionDecision",
    "PermissionManager",
    "PermissionMode",
    "PromptHandler",
]


# Legacy type alias (mirrors the TS PermissionDecision type).
PermissionDecision = str  # one of ApprovalDecision values


class PermissionMode(str, Enum):
    READ_ONLY = "read_only"
    AUTO = "auto"
    TRUST_COMMANDS = "trust_commands"
    FULL_ACCESS = "full_access"
    # Legacy callers can retain per-file approval; new sessions use AUTO.
    DEFAULT = "default"
    PLAN = "plan"


_PLAN_CONTROL_TOOLS = frozenset({"ask_user", "task", "exit_plan_mode"})
_PLAN_FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})

# Prefix applied by ``pepsicode.mcp`` to every MCP-backed tool alias.
MCP_TOOL_PREFIX = "mcp__"

# How much of an MCP tool's input to show in the approval prompt.
_MCP_INPUT_PREVIEW_CHARS = 600


def _normalize_path(target_path: str) -> str:
    return str(Path(target_path).resolve())


def _command_grant_key(command: str, args: list[str], cwd: str) -> str:
    return json.dumps([_normalize_path(cwd), command, args], ensure_ascii=False)


def _is_within_directory(root: str, target: str) -> bool:
    """Check if target is within root directory (case-insensitive on Windows)."""
    import os
    import sys

    try:
        resolved_target = Path(target).resolve()
        resolved_root = Path(root).resolve()
        if sys.platform == "win32":
            target_str = str(resolved_target).lower()
            root_str = str(resolved_root).lower()
            return target_str == root_str or target_str.startswith(root_str + os.sep)
        resolved_target.relative_to(resolved_root)
        return True
    except ValueError:
        return False


class PermissionManager:
    """Facade combining approval, sandbox, and Plan-mode boundary control.

    The manager owns:
      * ``self.approval``  — an :class:`ApprovalBackend` (who decides).
      * ``self.sandbox``   — a :class:`SandboxBackend` (how to confine).
      * ``self.store``     — an :class:`ApprovalStore` (what's already decided).
      * Plan-mode state (``self.mode``, ``self.plan_file_path``, ...).
    """

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        workspace_root: str,
        prompt: PromptHandler | None = None,
        *,
        approval: ApprovalBackend | None = None,
        sandbox: SandboxBackend | None = None,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        process_sandbox_mode: str = "danger-full-access",
    ) -> None:
        self.workspace_root = _normalize_path(workspace_root)
        self.approval: ApprovalBackend = approval or LocalApprovalBackend(prompt)
        self.sandbox: SandboxBackend = sandbox or LocalSandboxBackend()
        from pepsicode.sandbox.types import SandboxMode

        self.process_sandbox_mode = SandboxMode(process_sandbox_mode)
        self._operation_paths = ContextVar(f"permission_paths_{id(self)}", default=None)
        self._process_lock = threading.RLock()
        self._processes = set()
        self.store = ApprovalStore(workspace_root)
        self._task_command_grants = set()
        self.approval_metrics = {"requests": 0, "decisions": 0, "wait_seconds": 0.0}
        self._approval_metrics_lock = threading.Lock()

        # Plan-mode state
        self.mode = PermissionMode(mode)
        self.previous_mode = self.mode
        self.plan_file_path: str | None = None
        self._plan_followup: str | None = None

    @contextmanager
    def operation(self):
        """Path approvals cover one tool operation and one access intent only."""
        token = self._operation_paths.set(set())
        try:
            yield
        finally:
            self._operation_paths.reset(token)

    def close(self):
        """Drain every process owned by this permission/session instance."""
        from pepsicode.execution.process import stop

        with self._process_lock:
            children = list(self._processes)
            self._processes.clear()
        failures = []
        for process in children:
            job = getattr(process, "_pepsi_job", None)
            try:
                with process._pepsi_stop_lock:
                    try:
                        stop(process, job)
                    finally:
                        if job is not None:
                            job.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]

    # ------------------------------------------------------------------ #
    # Backward-compatible prompt property (delegates to LocalApprovalBackend)
    # ------------------------------------------------------------------ #

    @property
    def prompt(self) -> PromptHandler | None:
        """Legacy accessor for the underlying prompt handler.

        Returns the handler if the approval backend is a
        :class:`LocalApprovalBackend`, otherwise ``None``.
        """
        if isinstance(self.approval, LocalApprovalBackend):
            return self.approval.prompt
        return None

    @prompt.setter
    def prompt(self, value: PromptHandler | None) -> None:
        if isinstance(self.approval, LocalApprovalBackend):
            self.approval.prompt = value
        elif value is not None:
            # Replace the backend with a local one carrying the new handler.
            self.approval = LocalApprovalBackend(value)
        # If value is None and backend is not LocalApprovalBackend, no-op.

    # ------------------------------------------------------------------ #
    # Access presets and the independent Plan boundary
    # ------------------------------------------------------------------ #

    @property
    def access_mode(self) -> PermissionMode:
        return self.previous_mode if self.is_plan_mode else self.mode

    def set_access_mode(self, mode: str) -> None:
        self._task_command_grants.clear()
        candidate = PermissionMode(mode)
        if candidate not in {
            PermissionMode.READ_ONLY,
            PermissionMode.AUTO,
            PermissionMode.TRUST_COMMANDS,
            PermissionMode.FULL_ACCESS,
        }:
            raise ValueError("Choose read_only, auto, trust_commands, or full_access")
        if self.is_plan_mode:
            self.previous_mode = candidate
        else:
            self.mode = candidate

    @property
    def is_plan_mode(self) -> bool:
        return self.mode == PermissionMode.PLAN

    def enter_plan_mode(self) -> str:
        """Enter plan mode and return the sole writable plan path."""
        if not self.is_plan_mode:
            self._task_command_grants.clear()
            self.previous_mode = self.mode
            stamp = time.strftime("%Y%m%d-%H%M%S")
            suffix = uuid.uuid4().hex[:8]
            plan_path = Path(self.workspace_root) / ".pepsi-code" / "plans" / f"plan-{stamp}-{suffix}.md"
            self.plan_file_path = str(plan_path.resolve())
            if not _is_within_directory(self.workspace_root, self.plan_file_path):
                self.plan_file_path = None
                raise RuntimeError("Plan directory resolves outside the workspace")
            self.mode = PermissionMode.PLAN
        assert self.plan_file_path is not None
        return self.plan_file_path

    def restore_plan_state(self, mode: str | None, plan_file_path: str | None) -> None:
        """Restore a persisted plan state after validating its workspace path."""
        if mode != PermissionMode.PLAN.value or not plan_file_path:
            return
        candidate = _normalize_path(plan_file_path)
        plans_root = _normalize_path(str(Path(self.workspace_root) / ".pepsi-code" / "plans"))
        if not _is_within_directory(plans_root, candidate):
            return
        self.previous_mode = self.access_mode
        self.mode = PermissionMode.PLAN
        self.plan_file_path = candidate

    def _is_exact_plan_file(self, target_path: str) -> bool:
        if not self.plan_file_path or not target_path:
            return False
        import os

        return os.path.normcase(_normalize_path(target_path)) == os.path.normcase(_normalize_path(self.plan_file_path))

    def ensure_tool_allowed(self, tool: Any, parsed: Any) -> None:
        """Enforce read-only boundaries before requesting tool approval."""
        tool_name = str(getattr(tool, "name", ""))
        is_read_only = bool(getattr(tool, "is_read_only", False))

        if self.mode == PermissionMode.READ_ONLY and not is_read_only:
            if tool_name == "ask_user":
                return
            if (
                tool_name == "task"
                and isinstance(parsed, dict)
                and parsed.get("agent_type", "explore") in {"explore", "plan"}
            ):
                return
            raise RuntimeError(f"Read-only mode denied tool '{tool_name}'. Switch to auto to make changes.")

        # Reject Plan-disallowed tools before requesting approval for them.
        if self.is_plan_mode and tool_name.startswith(MCP_TOOL_PREFIX) and not is_read_only:
            raise RuntimeError(f"Plan mode denied tool '{tool_name}'. Only read-only MCP tools are allowed.")

        if tool_name.startswith(MCP_TOOL_PREFIX) and not is_read_only:
            outcome = self.check_mcp_tool(tool_name, parsed)
            if not outcome.is_allowed:
                raise PermissionError(outcome.denial_message(scope=tool_name))
            return outcome

        if not self.is_plan_mode:
            return

        if tool_name in _PLAN_CONTROL_TOOLS:
            if tool_name == "task" and isinstance(parsed, dict):
                agent_type = str(parsed.get("agent_type", "explore")).lower()
                if agent_type not in {"explore", "plan"}:
                    raise RuntimeError("Plan mode only allows explore or plan sub-agents")
            return

        if is_read_only:
            return

        if tool_name in _PLAN_FILE_WRITE_TOOLS and isinstance(parsed, dict):
            target = str(parsed.get("path") or parsed.get("file_path") or "")
            if self._is_exact_plan_file(target):
                return

        raise RuntimeError(
            f"Plan mode denied tool '{tool_name}'. Only read-only tools, explore/plan sub-agents, "
            "ask_user, exit_plan_mode, and writes to the active plan file are allowed."
        )

    def ensure_local_command_allowed(self, user_input: str) -> None:
        """Block local slash-command side doors while Plan mode is active."""
        if self.mode == PermissionMode.READ_ONLY:
            parts = user_input.casefold().split()
            if parts[:2] in (
                ["/worktree", "create"],
                ["/worktree", "exit"],
                ["/worktree", "cleanup"],
                ["/hooks", "trust"],
            ):
                raise RuntimeError(f"Read-only mode denied local command: {user_input}")
        if not self.is_plan_mode:
            return
        text = user_input.strip()
        command_parts = text.casefold().split()
        is_hook_trust = len(command_parts) >= 2 and command_parts[:2] == ["/hooks", "trust"]
        blocked = (
            (
                bool(command_parts)
                and command_parts[0] == "/model"
                and len(command_parts) > 1
                and command_parts[1:] not in (["list"], ["refresh"])
            )
            or (bool(command_parts) and command_parts[0] == "/effort" and len(command_parts) > 1)
            or (
                bool(command_parts)
                and command_parts[0] == "/provider"
                and len(command_parts) > 1
                and command_parts[1:] != ["list"]
            )
            or text.startswith("/transcript-save")
            or text.startswith("/resume")
            or text.startswith("/worktree create")
            or text.startswith("/worktree enter")
            or text.startswith("/worktree exit")
            or text.startswith("/worktree cleanup")
            or is_hook_trust
        )
        if blocked:
            raise RuntimeError(f"Plan mode denied local command: {text}")

    def request_plan_approval(self) -> dict[str, str]:
        """Show the active plan for approval and update the mode atomically."""
        if not self.is_plan_mode:
            return {"status": "error", "message": "Not currently in Plan mode."}
        if not self.plan_file_path:
            return {"status": "error", "message": "No active plan file."}
        plan_path = Path(self.plan_file_path)
        if not plan_path.is_file():
            return {"status": "error", "message": "The plan file does not exist yet."}
        content = plan_path.read_text(encoding="utf-8").strip()
        if not content:
            return {"status": "error", "message": "The plan file is empty."}

        req = ApprovalRequest(
            kind=ApprovalKind.PLAN,
            summary="Review the implementation plan",
            details=[f"plan: {self.plan_file_path}", "", content],
            scope=self.plan_file_path,
            choices=[
                {"key": "1", "label": "approve and execute", "decision": "allow_once"},
                {"key": "2", "label": "request changes", "decision": "deny_with_feedback"},
                {"key": "3", "label": "keep planning", "decision": "deny_once"},
            ],
        )
        outcome = self._request_approval(req)

        if outcome.is_unavailable:
            return {
                "status": "error",
                "message": "Plan approval requires an interactive terminal; Plan mode remains active.",
            }
        if outcome.decision == ApprovalDecision.ALLOW_ONCE:
            self.mode = self.previous_mode
            self._plan_followup = f"The plan has been approved. Execute it now:\n\n{content}"
            return {"status": "approved", "message": "Plan approved. Exiting Plan mode."}
        if outcome.decision == ApprovalDecision.DENY_WITH_FEEDBACK:
            feedback = outcome.feedback
            if feedback:
                self._plan_followup = f"Revise the plan using this feedback:\n\n{feedback}"
                return {"status": "feedback", "message": "Plan changes requested."}
        return {"status": "pending", "message": "Plan was not approved; Plan mode remains active."}

    def consume_plan_followup(self) -> str | None:
        followup = self._plan_followup
        self._plan_followup = None
        return followup

    # ------------------------------------------------------------------ #
    # Turn lifecycle (delegates to store)
    # ------------------------------------------------------------------ #

    def begin_turn(self) -> None:
        self._task_command_grants.clear()
        self.store.begin_turn()

    def end_turn(self) -> None:
        self._task_command_grants.clear()
        self.store.end_turn()

    def clear_task_grants(self):
        self._task_command_grants.clear()

    def _request_approval(self, request):
        started = time.monotonic()
        with self._approval_metrics_lock:
            self.approval_metrics["requests"] += 1
        try:
            outcome = self.approval.request(request)
            if not outcome.is_unavailable:
                with self._approval_metrics_lock:
                    self.approval_metrics["decisions"] += 1
            return outcome
        finally:
            with self._approval_metrics_lock:
                self.approval_metrics["wait_seconds"] += time.monotonic() - started

    def _task_command_key(self, command, args, cwd, environment=None, verification=False):
        from pepsicode.core.task_control import snapshot
        from pepsicode.core.workspace_state import digest, task_paths, workspace_state
        from pepsicode.execution.runtime import current_scope

        scope = current_scope()
        task = snapshot().get("task")
        if not scope or scope.parent_task_id is not None or not task or task["status"] != "active":
            return None
        state = workspace_state(self.workspace_root, include_paths=task_paths(task), reason="command_task_grant")
        environment = dict(os.environ if environment is None else environment)
        executable = shutil.which(command, path=environment.get("PATH"))
        if executable is None:
            return None
        executable = str(Path(executable).resolve())
        executable_version = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        if verification:
            environment.pop("PYTHONPYCACHEPREFIX", None)
        return digest(
            [
                task["id"],
                task["revision"],
                self.mode.value,
                self.workspace_root,
                _normalize_path(cwd),
                command,
                args,
                state["digest"],
                executable,
                executable_version,
                environment,
            ]
        )

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #

    def get_summary(self) -> list[str]:
        summary = [f"cwd: {self.workspace_root}", f"mode: {self.mode.value}", f"access mode: {self.access_mode.value}"]
        if self.plan_file_path:
            summary.append(f"plan: {self.plan_file_path}")
        summary.extend(self.store.get_summary_lines())
        summary.append(
            f"approval requests: {self.approval_metrics['requests']}; decisions: {self.approval_metrics['decisions']}; backend wait: {self.approval_metrics['wait_seconds']:.3f}s"
        )
        return summary

    # ------------------------------------------------------------------ #
    # New typed check_* methods (return ApprovalOutcome)
    # ------------------------------------------------------------------ #

    def check_path_access(self, target_path: str, intent: str) -> ApprovalOutcome:
        """Check whether a path can be accessed, returning a typed outcome."""
        normalized = _normalize_path(target_path)

        if self.is_plan_mode and intent not in {"read", "list", "search"} and not self._is_exact_plan_file(normalized):
            return ApprovalOutcome(ApprovalDecision.DENY_ONCE)
        operation_paths = self._operation_paths.get()
        if operation_paths is not None and (self.mode, normalized, intent) in operation_paths:
            if self.mode != PermissionMode.READ_ONLY or intent in {"read", "list", "search"}:
                return ApprovalOutcome(ApprovalDecision.ALLOW_ONCE)

        if self.mode == PermissionMode.READ_ONLY and intent not in {"read", "list", "search"}:
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)
        if self.mode == PermissionMode.FULL_ACCESS:
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        # 1. Check cache
        cached = self.store.lookup(ApprovalKind.PATH, normalized, intent=intent)
        if cached is not None:
            return cached

        # 2. No cached decision — need to prompt
        if self.prompt is None and not isinstance(self.approval, LocalApprovalBackend):
            # Non-local backend: still try it (may be remote/automated)
            pass
        elif self.prompt is None:
            return ApprovalOutcome(decision=ApprovalDecision.UNAVAILABLE)

        scope_directory = normalized if intent in {"list", "command_cwd"} else str(Path(normalized).parent)
        req = ApprovalRequest(
            kind=ApprovalKind.PATH,
            summary=f"pepsicode wants {intent.replace('_', ' ')} access outside the current cwd",
            details=[
                f"cwd: {self.workspace_root}",
                f"target: {normalized}",
                f"scope directory: {scope_directory}",
            ],
            scope=scope_directory,
            choices=[
                {"key": "y", "label": "allow once", "decision": "allow_once"},
                {"key": "a", "label": "allow this directory", "decision": "allow_always"},
                {"key": "n", "label": "deny once", "decision": "deny_once"},
                {"key": "d", "label": "deny this directory", "decision": "deny_always"},
            ],
            metadata={"intent": intent, "target": normalized},
        )
        outcome = self._request_approval(req)
        self.store.record(ApprovalKind.PATH, normalized, outcome)
        if operation_paths is not None and outcome.is_allowed:
            operation_paths.add((self.mode, normalized, intent))
        return outcome

    def check_command(
        self,
        command: str,
        args: list[str],
        command_cwd: str,
        force_prompt_reason: str | None = None,
        environment=None,
        verification=False,
    ) -> ApprovalOutcome:
        """Check whether a command can run, returning a typed outcome."""
        if self.mode == PermissionMode.READ_ONLY or self.is_plan_mode:
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)
        if self.mode == PermissionMode.FULL_ACCESS:
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)
        # 1. Path check on cwd
        path_outcome = self.check_path_access(command_cwd, "command_cwd")
        if path_outcome.is_denied or path_outcome.is_unavailable:
            return path_outcome

        if self.mode == PermissionMode.TRUST_COMMANDS:
            # Trust applies to processes, but an explicit denial for this exact
            # command and working directory remains authoritative.
            grant_key = _command_grant_key(command, args, command_cwd)
            cached = self.store.lookup(ApprovalKind.COMMAND, grant_key)
            if cached is not None and cached.is_denied:
                return cached
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        # 2. Dangerous-command classification
        reason = force_prompt_reason or classify_dangerous_command(command, args)
        if not reason:
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        # 3. Check cache
        signature = format_command_signature(command, args)
        grant_key = _command_grant_key(command, args, command_cwd)
        try:
            task_key = self._task_command_key(command, args, command_cwd, environment, verification)
        except (OSError, ValueError):
            task_key = None
        cached = self.store.lookup(ApprovalKind.COMMAND, grant_key)
        if cached is not None and cached.is_denied:
            return cached
        if task_key and task_key in self._task_command_grants:
            return ApprovalOutcome(ApprovalDecision.ALLOW_TASK)
        if cached is not None:
            return cached

        # 4. Prompt
        if self.prompt is None and isinstance(self.approval, LocalApprovalBackend):
            return ApprovalOutcome(decision=ApprovalDecision.UNAVAILABLE)

        summary = (
            "pepsicode wants to run a dangerous command"
            if not force_prompt_reason
            else "pepsicode wants approval for this command"
        )
        req = ApprovalRequest(
            kind=ApprovalKind.COMMAND,
            summary=summary,
            details=[f"cwd: {command_cwd}", f"command: {signature}", f"reason: {reason}"],
            scope=signature,
            choices=[
                {"key": "y", "label": "allow once", "decision": "allow_once"},
                *(
                    [
                        {
                            "key": "t",
                            "label": "allow this exact command for this task and current inputs",
                            "decision": "allow_task",
                        }
                    ]
                    if task_key
                    else []
                ),
                {"key": "a", "label": "always allow this command", "decision": "allow_always"},
                {"key": "n", "label": "deny once", "decision": "deny_once"},
                {"key": "d", "label": "always deny this command", "decision": "deny_always"},
            ],
            metadata={"cwd": command_cwd, "reason": reason},
        )
        outcome = self._request_approval(req)
        if outcome.decision == ApprovalDecision.ALLOW_TASK:
            if (
                task_key is None
                or self._task_command_key(command, args, command_cwd, environment, verification) != task_key
            ):
                return ApprovalOutcome(ApprovalDecision.DENY_ONCE)
            self._task_command_grants.add(task_key)
        self.store.record(ApprovalKind.COMMAND, grant_key, outcome)
        return outcome

    def check_mcp_tool(self, tool_name: str, tool_input: Any) -> ApprovalOutcome:
        """Check whether an MCP-backed tool may run, returning a typed outcome.

        MCP servers are third-party code, so a tool that is not declared
        read-only in the server config needs an explicit decision.  The scope
        is the wrapped tool name, so ``allow_always`` trusts that one tool
        rather than the whole server.
        """
        if self.mode == PermissionMode.READ_ONLY or self.is_plan_mode:
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)
        if self.mode == PermissionMode.FULL_ACCESS:
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        # 1. Check cache
        cached = self.store.lookup(ApprovalKind.MCP, tool_name)
        if cached is not None:
            return cached

        # 2. Prompt
        if self.prompt is None and isinstance(self.approval, LocalApprovalBackend):
            return ApprovalOutcome(decision=ApprovalDecision.UNAVAILABLE)

        try:
            import json

            rendered_input = json.dumps(tool_input, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered_input = str(tool_input)
        if len(rendered_input) > _MCP_INPUT_PREVIEW_CHARS:
            rendered_input = rendered_input[:_MCP_INPUT_PREVIEW_CHARS] + "\n... (truncated)"

        req = ApprovalRequest(
            kind=ApprovalKind.MCP,
            summary="pepsicode wants to call an MCP server tool",
            details=[f"tool: {tool_name}", "", rendered_input],
            scope=tool_name,
            choices=[
                {"key": "y", "label": "allow once", "decision": "allow_once"},
                {"key": "a", "label": "always allow this MCP tool", "decision": "allow_always"},
                {"key": "n", "label": "deny once", "decision": "deny_once"},
                {"key": "d", "label": "always deny this MCP tool", "decision": "deny_always"},
            ],
            metadata={"tool": tool_name},
        )
        outcome = self._request_approval(req)
        self.store.record(ApprovalKind.MCP, tool_name, outcome)
        return outcome

    def check_external_action(self, operation: str, target: str) -> ApprovalOutcome:
        """Authorize one exact external mutation; grants never change standing policy."""
        if self.mode in {PermissionMode.READ_ONLY, PermissionMode.PLAN}:
            return ApprovalOutcome(ApprovalDecision.DENY_ONCE)
        if self.mode == PermissionMode.FULL_ACCESS:
            return ApprovalOutcome(ApprovalDecision.ALLOW_ONCE)
        return self._request_approval(
            ApprovalRequest(
                kind=ApprovalKind.MCP,
                summary="pepsicode wants to change an external resource",
                details=[f"operation: {operation}", f"target: {target}"],
                scope=f"{operation} {target}",
                choices=[
                    {"key": "y", "label": "allow this action once", "decision": "allow_once"},
                    {"key": "n", "label": "deny", "decision": "deny_once"},
                ],
            )
        )

    def check_edit(self, target_path: str, diff_preview: str) -> ApprovalOutcome:
        """Check whether an edit can be applied, returning a typed outcome."""
        normalized = _normalize_path(target_path)

        # Plan-file writes bypass approval
        if self.is_plan_mode and self._is_exact_plan_file(normalized):
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)
        if self.mode == PermissionMode.READ_ONLY or self.is_plan_mode:
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)
        if self.mode == PermissionMode.FULL_ACCESS:
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        # 1. Check cache
        cached = self.store.lookup(ApprovalKind.EDIT, normalized)
        if cached is not None:
            return cached

        if self.mode in {PermissionMode.AUTO, PermissionMode.TRUST_COMMANDS}:
            # Path approval already expresses consent to edit outside the
            # workspace; do not ask again for the same file modification.
            return self.check_path_access(normalized, "write")

        # 2. Prompt
        if self.prompt is None and isinstance(self.approval, LocalApprovalBackend):
            return ApprovalOutcome(decision=ApprovalDecision.UNAVAILABLE)

        req = ApprovalRequest(
            kind=ApprovalKind.EDIT,
            summary="pepsicode wants to apply a file modification",
            details=[f"target: {normalized}", "", diff_preview],
            scope=normalized,
            choices=[
                {"key": "1", "label": "apply once", "decision": "allow_once"},
                {"key": "2", "label": "allow this file in this turn", "decision": "allow_turn"},
                {"key": "3", "label": "allow all edits in this turn", "decision": "allow_all_turn"},
                {"key": "4", "label": "always allow this file", "decision": "allow_always"},
                {"key": "5", "label": "reject once", "decision": "deny_once"},
                {"key": "6", "label": "reject and send guidance to model", "decision": "deny_with_feedback"},
                {"key": "7", "label": "always reject this file", "decision": "deny_always"},
            ],
            metadata={"target": normalized},
        )
        outcome = self._request_approval(req)
        self.store.record(ApprovalKind.EDIT, normalized, outcome)
        return outcome

    # ------------------------------------------------------------------ #
    # Legacy ensure_* wrappers (raise RuntimeError on denial)
    # ------------------------------------------------------------------ #

    def ensure_path_access(self, target_path: str, intent: str) -> None:
        """Legacy wrapper: raises ``RuntimeError`` on denial."""
        outcome = self.check_path_access(target_path, intent)
        if outcome.is_denied or outcome.is_unavailable:
            raise RuntimeError(outcome.denial_message(scope=target_path))

    def ensure_command(
        self,
        command: str,
        args: list[str],
        command_cwd: str,
        force_prompt_reason: str | None = None,
    ) -> None:
        """Legacy wrapper: raises ``RuntimeError`` on denial."""
        outcome = self.check_command(command, args, command_cwd, force_prompt_reason)
        if outcome.is_denied or outcome.is_unavailable:
            signature = format_command_signature(command, args)
            raise RuntimeError(outcome.denial_message(scope=signature))

    def ensure_edit(self, target_path: str, diff_preview: str) -> None:
        """Legacy wrapper: raises ``RuntimeError`` on denial."""
        outcome = self.check_edit(target_path, diff_preview)
        if outcome.is_denied or outcome.is_unavailable:
            raise RuntimeError(outcome.denial_message(scope=target_path))
