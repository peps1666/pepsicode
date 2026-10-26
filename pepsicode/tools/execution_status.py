"""Inspect unresolved operations and explicitly reconcile observed external state."""

from __future__ import annotations

import json

from pepsicode.execution.runtime import current_scope
from pepsicode.tooling import ToolContext, ToolDefinition, ToolResult


def _validate(value):
    action = value.get("action", "inspect")
    if action not in {"inspect", "reconcile"}:
        raise ValueError("action must be inspect or reconcile")
    evidence = value.get("evidence", "")
    if not isinstance(evidence, str) or (action == "reconcile" and not evidence.strip()):
        raise ValueError("reconcile requires evidence from inspecting the affected state")
    return {"action": action, "evidence": evidence.strip()}


def _run(value, context: ToolContext):
    scope = context.execution or current_scope()
    if scope is None:
        return ToolResult(False, "No active execution")
    if value["action"] == "reconcile" and scope.has_unknown:
        if context.permissions is None:
            raise PermissionError("Reconciliation requires explicit approval")
        calls = set(scope.unresolved)
        mode = context.permissions.mode
        target = json.dumps({"calls": sorted(calls), "evidence": value["evidence"]}, ensure_ascii=False)
        decision = context.permissions.check_external_action("reconcile unknown execution outcomes", target)
        if not decision.is_allowed:
            return ToolResult(False, decision.denial_message(scope="reconciliation"), status="blocked")
        if context.permissions.mode != mode or calls != set(scope.unresolved):
            raise PermissionError("Pending operations or permissions changed during reconciliation approval")
        scope.reconcile(value["evidence"])
    return ToolResult(
        True, json.dumps({**scope.snapshot(), "unresolved": list(scope.unresolved.values())}, ensure_ascii=False)
    )


execution_status_tool = ToolDefinition(
    name="execution_status",
    description="Inspect unresolved operations. After reading actual affected state, reconcile with evidence and explicit approval to unblock mutations. Does not replay any operation.",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "reconcile"]},
            "evidence": {"type": "string"},
        },
    },
    validator=_validate,
    run=_run,
    affects_workspace=False,
    always_loaded=True,
)
