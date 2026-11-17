from __future__ import annotations

from pathlib import Path

from pepsicode.execution.runtime import current_scope
from pepsicode.tooling import ToolContext


def resolve_tool_path(context: ToolContext, input_path: str, intent: str) -> Path:
    candidate = Path(input_path)
    target = candidate if candidate.is_absolute() else Path(context.cwd) / candidate
    normalized = target.resolve()
    scope = context.execution or current_scope()
    if scope and scope.workspace_boundary and not normalized.is_relative_to(Path(scope.workspace_boundary)):
        raise PermissionError(f"Path escapes the Worker checkout: {input_path}")

    if context.permissions is not None:
        outcome = context.permissions.check_path_access(str(normalized), intent)
        if outcome.is_denied or outcome.is_unavailable:
            raise PermissionError(outcome.denial_message(scope=str(normalized)))
    else:
        # Fallback: block paths that escape the workspace when no permissions manager
        workspace_root = Path(context.cwd).resolve()
        try:
            normalized.relative_to(workspace_root)
        except ValueError:
            raise PermissionError(f"Path escapes workspace: {input_path}")

    return normalized
