"""Inspect unresolved operations and explicitly reconcile observed external state."""

from __future__ import annotations

import json
import uuid

from pepsicode.execution.filesystem import FILES
from pepsicode.execution.runtime import current_scope
from pepsicode.tooling import ToolContext, ToolDefinition, ToolResult


def _observe(context, path):
    from pepsicode.core.workspace_state import digest

    target = FILES._path(context, path, "read")
    if target.is_dir():
        entries = sorted((p.name, p.lstat().st_mode) for p in target.iterdir())
        return {
            "kind": "directory_entries",
            "path": str(target),
            "version": digest(entries),
            "exists": True,
            "content": json.dumps(entries, ensure_ascii=False)[:8000],
        }
    file = FILES.snapshot(context, path, observe=False)
    return {
        "kind": "file",
        "path": str(file.path),
        "version": file.version,
        "exists": file.content is not None,
        "content": (file.content or b"").decode("utf-8", errors="replace")[:8000],
    }


def _validate(value):
    action = value.get("action", "inspect")
    if action not in {"inspect", "observe", "reconcile"}:
        raise ValueError("action must be inspect, observe, or reconcile")
    if action == "observe" and (not isinstance(value.get("path"), str) or not value["path"]):
        raise ValueError("observe requires a path")
    if action == "reconcile":
        if not isinstance(value.get("call_id"), str) or not value["call_id"]:
            raise ValueError("reconcile requires one explicit call_id")
        if isinstance(value.get("revision"), bool) or not isinstance(value.get("revision"), int):
            raise ValueError("reconcile requires the current recovery revision")
        ids = value.get("observation_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise ValueError("reconcile requires recorded observation_ids")
        if value.get("outcome") not in {"occurred", "not_occurred", "unknown"}:
            raise ValueError("outcome must be occurred, not_occurred, or unknown")
        if not isinstance(value.get("evidence"), str) or not value["evidence"].strip():
            raise ValueError("reconcile requires an explanation of the observed evidence")
    return value


def _run(value, context: ToolContext):
    scope = context.execution or current_scope()
    if scope is None:
        return ToolResult(False, "No active execution")
    action = value.get("action", "inspect")
    if action == "observe":
        observed = _observe(context, value["path"])
        content = observed.pop("content")
        observation = {
            "id": uuid.uuid4().hex,
            **observed,
            "recovery_revision": scope.recovery_version[0],
        }
        scope.record("recovery/observed", observation=observation)
        scope.observations[observation["id"]] = observation
        return ToolResult(True, json.dumps({**observation, "content": content}, ensure_ascii=False))
    if action == "reconcile":
        call_id, revision = value["call_id"], value["revision"]
        if call_id not in scope.unresolved or revision != scope.recovery_version[0]:
            raise PermissionError("Stale recovery revision or unknown call_id")
        observations = []
        for identity in value["observation_ids"]:
            item = scope.observations.get(identity)
            if not item or item["recovery_revision"] != revision:
                raise PermissionError("Observation is missing or stale")
            if _observe(context, item["path"])["version"] != item["version"]:
                raise PermissionError("Observed state changed; observe again")
            observations.append(item)
        evidence = {"explanation": value["evidence"], "observations": observations, "outcome": value["outcome"]}
        if context.permissions is None:
            raise PermissionError("Reconciliation requires explicit approval")
        mode = context.permissions.mode
        target = json.dumps({"call_id": call_id, "revision": revision, **evidence}, ensure_ascii=False)
        decision = context.permissions.check_external_action("reconcile one unknown execution outcome", target)
        if not decision.is_allowed:
            return ToolResult(False, decision.denial_message(scope="reconciliation"), status="blocked")
        if context.permissions.mode != mode or scope.recovery_version[0] != revision:
            raise PermissionError("Pending operations or permissions changed during reconciliation approval")
        for item in observations:
            if _observe(context, item["path"])["version"] != item["version"]:
                raise PermissionError("Observed state changed during approval")
        scope.reconcile(call_id, revision, evidence, value["outcome"])
    return ToolResult(
        True, json.dumps({**scope.snapshot(), "unresolved": list(scope.unresolved.values())}, ensure_ascii=False)
    )


execution_status_tool = ToolDefinition(
    name="execution_status",
    description="Inspect unresolved calls; observe files without launching programs. Reconcile ONE call_id at the exact recovery revision, citing current observation_ids and occurred/not_occurred/unknown. Requires approval. Never replays an operation; unknown retains the block.",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "observe", "reconcile"]},
            "path": {"type": "string"},
            "call_id": {"type": "string"},
            "revision": {"type": "integer"},
            "observation_ids": {"type": "array", "items": {"type": "string"}},
            "outcome": {"type": "string", "enum": ["occurred", "not_occurred", "unknown"]},
            "evidence": {"type": "string"},
        },
    },
    validator=_validate,
    run=_run,
    affects_workspace=False,
    always_loaded=True,
)
