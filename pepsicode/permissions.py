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

import time
import uuid
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
    DEFAULT = "default"
    PLAN = "plan"


_PLAN_CONTROL_TOOLS = frozenset({"ask_user", "task", "exit_plan_mode"})
_PLAN_FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def _normalize_path(target_path: str) -> str:
    return str(Path(target_path).resolve())


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
    ) -> None:
        self.workspace_root = _normalize_path(workspace_root)
        self.approval: ApprovalBackend = approval or LocalApprovalBackend(prompt)
        self.sandbox: SandboxBackend = sandbox or LocalSandboxBackend()
        self.store = ApprovalStore(workspace_root)

        # Plan-mode state
        self.mode = PermissionMode.DEFAULT
        self.previous_mode = PermissionMode.DEFAULT
        self.plan_file_path: str | None = None
        self._plan_followup: str | None = None

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
    # Plan mode (unchanged behaviour, preserved verbatim)
    # ------------------------------------------------------------------ #

    @property
    def is_plan_mode(self) -> bool:
        return self.mode == PermissionMode.PLAN

    def enter_plan_mode(self) -> str:
        """Enter plan mode and return the sole writable plan path."""
        if not self.is_plan_mode:
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
        self.previous_mode = PermissionMode.DEFAULT
        self.mode = PermissionMode.PLAN
        self.plan_file_path = candidate

    def _is_exact_plan_file(self, target_path: str) -> bool:
        if not self.plan_file_path or not target_path:
            return False
        import os

        return os.path.normcase(_normalize_path(target_path)) == os.path.normcase(_normalize_path(self.plan_file_path))

    def ensure_tool_allowed(self, tool: Any, parsed: Any) -> None:
        """Apply the non-bypassable Plan-mode capability boundary."""
        if not self.is_plan_mode:
            return

        tool_name = str(getattr(tool, "name", ""))
        if tool_name in _PLAN_CONTROL_TOOLS:
            if tool_name == "task" and isinstance(parsed, dict):
                agent_type = str(parsed.get("agent_type", "explore")).lower()
                if agent_type not in {"explore", "plan"}:
                    raise RuntimeError("Plan mode only allows explore or plan sub-agents")
            return

        if bool(getattr(tool, "is_read_only", False)):
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
        if not self.is_plan_mode:
            return
        text = user_input.strip()
        command_parts = text.casefold().split()
        is_hook_trust = len(command_parts) >= 2 and command_parts[:2] == ["/hooks", "trust"]
        blocked = (
            text.startswith("/model ")
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
        outcome = self.approval.request(req)

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
        self.store.begin_turn()

    def end_turn(self) -> None:
        self.store.end_turn()

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #

    def get_summary(self) -> list[str]:
        summary = [f"cwd: {self.workspace_root}", f"mode: {self.mode.value}"]
        if self.plan_file_path:
            summary.append(f"plan: {self.plan_file_path}")
        summary.extend(self.store.get_summary_lines())
        return summary

    # ------------------------------------------------------------------ #
    # New typed check_* methods (return ApprovalOutcome)
    # ------------------------------------------------------------------ #

    def check_path_access(self, target_path: str, intent: str) -> ApprovalOutcome:
        """Check whether a path can be accessed, returning a typed outcome."""
        normalized = _normalize_path(target_path)

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
        outcome = self.approval.request(req)
        self.store.record(ApprovalKind.PATH, normalized, outcome)
        return outcome

    def check_command(
        self,
        command: str,
        args: list[str],
        command_cwd: str,
        force_prompt_reason: str | None = None,
    ) -> ApprovalOutcome:
        """Check whether a command can run, returning a typed outcome."""
        # 1. Path check on cwd
        path_outcome = self.check_path_access(command_cwd, "command_cwd")
        if path_outcome.is_denied or path_outcome.is_unavailable:
            return path_outcome

        # 2. Dangerous-command classification
        reason = force_prompt_reason or classify_dangerous_command(command, args)
        if not reason:
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        # 3. Check cache
        signature = format_command_signature(command, args)
        cached = self.store.lookup(ApprovalKind.COMMAND, signature)
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
                {"key": "a", "label": "always allow this command", "decision": "allow_always"},
                {"key": "n", "label": "deny once", "decision": "deny_once"},
                {"key": "d", "label": "always deny this command", "decision": "deny_always"},
            ],
            metadata={"cwd": command_cwd, "reason": reason},
        )
        outcome = self.approval.request(req)
        self.store.record(ApprovalKind.COMMAND, signature, outcome)
        return outcome

    def check_edit(self, target_path: str, diff_preview: str) -> ApprovalOutcome:
        """Check whether an edit can be applied, returning a typed outcome."""
        normalized = _normalize_path(target_path)

        # Plan-file writes bypass approval
        if self.is_plan_mode and self._is_exact_plan_file(normalized):
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        # 1. Check cache
        cached = self.store.lookup(ApprovalKind.EDIT, normalized)
        if cached is not None:
            return cached

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
        outcome = self.approval.request(req)
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
