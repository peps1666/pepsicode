"""Task tool: delegate a scoped task to an isolated sub-agent.

Exposes the existing :mod:`pepsicode.sub_agents` definitions (Explore / Plan /
General) as a tool the model can call.  Each invocation runs in its own
context window with a restricted tool set, then returns only a compact summary
to the parent -- keeping the main conversation lean (Claude Code's AgentTool
pattern; see CoreCoder article 06-multi-agent).
"""

from __future__ import annotations

from typing import Any, Callable

from pepsicode.sub_agents import AgentDefinition, AgentType
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult

# Read-only tools available to every sub-agent.
from pepsicode.tools.read_file import read_file_tool
from pepsicode.tools.list_files import list_files_tool
from pepsicode.tools.grep_files import grep_files_tool
from pepsicode.tools.file_tree import file_tree_tool
from pepsicode.tools.code_nav import find_symbols_tool, find_references_tool, get_ast_info_tool

# Additional write/exec tools available to the general-purpose sub-agent.
from pepsicode.tools.write_file import write_file_tool
from pepsicode.tools.edit_file import edit_file_tool
from pepsicode.tools.patch_file import patch_file_tool
from pepsicode.tools.modify_file import modify_file_tool
from pepsicode.tools.multi_edit import multi_edit_tool
from pepsicode.tools.run_command import run_command_tool

_READ_ONLY_TOOLS = [
    read_file_tool,
    list_files_tool,
    grep_files_tool,
    file_tree_tool,
    find_symbols_tool,
    find_references_tool,
    get_ast_info_tool,
]

_GENERAL_EXTRA_TOOLS = [
    write_file_tool,
    edit_file_tool,
    patch_file_tool,
    modify_file_tool,
    multi_edit_tool,
    run_command_tool,
]

_AGENT_TYPES = {
    "explore": AgentType.EXPLORE,
    "plan": AgentType.PLAN,
    "general": AgentType.GENERAL,
}


def _build_sub_registry(definition: AgentDefinition) -> ToolRegistry:
    """Assemble a restricted tool registry for a sub-agent.

    Never includes the Task tool itself, so sub-agents cannot recurse.
    """
    tools = list(_READ_ONLY_TOOLS)
    if not definition.is_read_only:
        tools += _GENERAL_EXTRA_TOOLS
    return ToolRegistry(tools)


def create_task_tool(
    cwd: str,
    runtime: dict[str, Any] | None,
    model_factory: Callable[[ToolRegistry], Any] | None = None,
) -> ToolDefinition:
    """Build the Task tool.

    ``model_factory(registry) -> ModelAdapter`` lets callers inject a model
    (tests use a mock).  When omitted and a runtime is configured, a fresh
    Anthropic adapter bound to the sub-agent's restricted registry is used.
    """

    def _validate(input_data: dict) -> dict:
        agent_type = str(input_data.get("agent_type", "explore")).strip().lower()
        if agent_type not in _AGENT_TYPES:
            raise ValueError(f"agent_type must be one of {sorted(_AGENT_TYPES)}")
        task = input_data.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task is required")
        return {"agent_type": agent_type, "task": task.strip()}

    def _run(parsed: dict, context: ToolContext) -> ToolResult:
        # Lazy imports avoid any import cycle at module load time.
        from pepsicode.agent_loop import run_agent_turn

        agent_type = _AGENT_TYPES[parsed["agent_type"]]
        definition = {
            AgentType.EXPLORE: AgentDefinition.explore_agent,
            AgentType.PLAN: AgentDefinition.plan_agent,
            AgentType.GENERAL: AgentDefinition.general_agent,
        }[agent_type]()

        sub_registry = _build_sub_registry(definition)

        if model_factory is not None:
            sub_model = model_factory(sub_registry)
        elif runtime is not None:
            from pepsicode.anthropic_adapter import AnthropicModelAdapter
            sub_model = AnthropicModelAdapter(runtime, sub_registry)
        else:
            return ToolResult(
                ok=False,
                output="Task tool requires a configured model (no runtime available).",
            )

        sub_messages = [
            {"role": "system", "content": definition.system_prompt_template},
            {"role": "user", "content": parsed["task"]},
        ]
        try:
            result_messages = run_agent_turn(
                model=sub_model,
                tools=sub_registry,
                messages=sub_messages,
                cwd=context.cwd or cwd,
                permissions=context.permissions,
                max_steps=definition.max_turns,
            )
        except Exception as error:  # noqa: BLE001
            return ToolResult(ok=False, output=f"Sub-agent ({definition.name}) failed: {error}")

        final = next(
            (m["content"] for m in reversed(result_messages) if m.get("role") == "assistant"),
            "",
        )
        tool_calls = sum(1 for m in result_messages if m.get("role") == "assistant_tool_call")
        summary = (
            f"[Sub-agent {definition.name} completed | tool calls: {tool_calls}]\n\n{final}"
        )
        return ToolResult(ok=True, output=summary)

    return ToolDefinition(
        name="task",
        description=(
            "Delegate a scoped task to an isolated sub-agent that runs in its own "
            "context window and returns only a summary. agent_type: 'explore' "
            "(fast read-only search), 'plan' (thorough read-only analysis), or "
            "'general' (full read/write/exec). Use for broad codebase exploration "
            "or self-contained subtasks to keep the main context lean."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_type": {"type": "string", "enum": ["explore", "plan", "general"]},
                "task": {"type": "string", "description": "Self-contained task description for the sub-agent"},
            },
            "required": ["task"],
        },
        validator=_validate,
        run=_run,
    )
