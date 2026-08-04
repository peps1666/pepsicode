from __future__ import annotations

from pepsicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict | None) -> dict:
    if input_data is None:
        return {}
    if not isinstance(input_data, dict):
        raise ValueError("input must be an object")
    return {}


def _run(_input_data: dict, context) -> ToolResult:
    permissions = context.permissions
    if permissions is None or not getattr(permissions, "is_plan_mode", False):
        return ToolResult(
            ok=False,
            output="exit_plan_mode is only available while Plan mode is active.",
        )

    outcome = permissions.request_plan_approval()
    status = outcome.get("status", "error")
    message = outcome.get("message", "Unable to finish Plan mode.")
    if status == "error":
        return ToolResult(ok=False, output=message, awaitUser="interactive terminal" in message)
    return ToolResult(ok=True, output=message, awaitUser=True)


exit_plan_mode_tool = ToolDefinition(
    name="exit_plan_mode",
    description=(
        "Present the completed plan file for user approval and end the planning turn. "
        "Call this only after the plan file is complete; it must be the only tool call in the response."
    ),
    input_schema={"type": "object", "properties": {}},
    validator=_validate,
    run=_run,
)
