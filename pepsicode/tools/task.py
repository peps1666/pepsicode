"""Task tool: delegate a scoped task to an isolated sub-agent.

Exposes the existing :mod:`pepsicode.sub_agents` definitions (Explore / Plan /
General) as a tool the model can call.  Each invocation runs in its own
context window with a restricted tool set, then returns only a compact summary
to the parent -- keeping the main conversation lean (Claude Code's AgentTool
pattern; see CoreCoder article 06-multi-agent).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pepsicode.agents.loader import get_default_loader
from pepsicode.agents.tool_filter import resolve_agent_tools
from pepsicode.agents.trace import TraceManager
from pepsicode.sub_agents import AgentDefinition, AgentType
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.code_nav import find_references_tool, find_symbols_tool, get_ast_info_tool
from pepsicode.tools.edit_file import edit_file_tool
from pepsicode.tools.file_tree import file_tree_tool
from pepsicode.tools.grep_files import grep_files_tool
from pepsicode.tools.list_files import list_files_tool
from pepsicode.tools.modify_file import modify_file_tool
from pepsicode.tools.multi_edit import multi_edit_tool
from pepsicode.tools.patch_file import patch_file_tool

# Read-only tools available to every sub-agent.
from pepsicode.tools.read_file import read_file_tool
from pepsicode.tools.run_command import run_command_tool

# Additional write/exec tools available to the general-purpose sub-agent.
from pepsicode.tools.write_file import write_file_tool
from pepsicode.types import ChatMessage

# The pool of tools a sub-agent can ever access.  Individual agents get a
# filtered subset via :func:`resolve_agent_tools` based on their
# ``allowed_tools`` / ``disallowed_tools`` definition.  Kept explicit (rather
# than derived from the parent registry) so sub-agents never pick up MCP or
# other dynamic tools unless intentionally added here.
_SUB_AGENT_TOOL_POOL = ToolRegistry(
    [
        read_file_tool,
        list_files_tool,
        grep_files_tool,
        file_tree_tool,
        find_symbols_tool,
        find_references_tool,
        get_ast_info_tool,
        write_file_tool,
        edit_file_tool,
        patch_file_tool,
        modify_file_tool,
        multi_edit_tool,
        run_command_tool,
    ]
)

_AGENT_TYPES = {
    "explore": AgentType.EXPLORE,
    "plan": AgentType.PLAN,
    "general": AgentType.GENERAL,
    "verification": AgentType.GENERAL,  # specialized general agent
}

_FALLBACK_FACTORIES: dict[str, Callable[[], AgentDefinition]] = {
    "explore": AgentDefinition.explore_agent,
    "plan": AgentDefinition.plan_agent,
    "general": AgentDefinition.general_agent,
    "verification": AgentDefinition.verification_agent,
}


def _load_agent_definition(agent_name: str, cwd: str) -> AgentDefinition | None:
    """Load a markdown definition, with safe compatibility fallbacks."""
    definition = get_default_loader(cwd).get(agent_name)
    if definition is not None:
        return definition
    factory = _FALLBACK_FACTORIES.get(agent_name)
    return factory() if factory is not None else None


def _runtime_for_agent(runtime: dict[str, Any], definition: AgentDefinition) -> dict[str, Any]:
    """Return an isolated runtime config honoring the agent's model override."""
    child_runtime = dict(runtime)
    if definition.model and definition.model.lower() != "inherit":
        child_runtime["model"] = definition.model
    return child_runtime


def create_task_tool(
    cwd: str,
    runtime: dict[str, Any] | None,
    model_factory: Callable[[ToolRegistry], Any] | None = None,
    *,
    cost_tracker: Any | None = None,
    trace_manager: TraceManager | None = None,
    parent_trace_id: str | None = None,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str, bool], None] | None = None,
) -> ToolDefinition:
    """Build the Task tool.

    ``model_factory(registry) -> ModelAdapter`` lets callers inject a model
    (tests use a mock).  When omitted and a runtime is configured, a fresh
    Anthropic adapter bound to the sub-agent's restricted registry is used.

    When ``cost_tracker`` and ``trace_manager`` are provided the sub-agent's
    token usage and tool calls are recorded -- both on the trace node and on
    the shared cost tracker so the parent session's budget guard sees them.
    ``on_tool_start`` / ``on_tool_result`` are forwarded to the sub-agent loop
    so callers can surface sub-agent progress to the user.
    """

    def _validate(input_data: dict) -> dict:
        agent_type = str(input_data.get("agent_type", "explore")).strip().lower()
        if not agent_type:
            raise ValueError("agent_type must not be empty")
        if _load_agent_definition(agent_type, cwd) is None:
            available = sorted(set(_AGENT_TYPES) | set(get_default_loader(cwd).list_names()))
            raise ValueError(f"unknown agent_type {agent_type!r}; available agents: {available}")
        task = input_data.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task is required")
        return {"agent_type": agent_type, "task": task.strip()}

    def _run(parsed: dict, context: ToolContext) -> ToolResult:
        # Lazy imports avoid any import cycle at module load time.
        from pepsicode.agent_loop import run_agent_turn

        agent_key = parsed["agent_type"]

        # Load the agent definition from markdown files (hot-reloadable).
        # Falls back to the built-in AgentDefinition classmethods when the
        # loader cannot find a file, so existing behavior is preserved.
        effective_cwd = context.cwd or cwd
        definition = _load_agent_definition(agent_key, effective_cwd)
        if definition is None:
            return ToolResult(ok=False, output=f"Agent definition not found: {agent_key}")

        sub_registry = resolve_agent_tools(_SUB_AGENT_TOOL_POOL, definition)

        if model_factory is not None:
            sub_model = model_factory(sub_registry)
        elif runtime is not None:
            from pepsicode.anthropic_adapter import AnthropicModelAdapter

            sub_model = AnthropicModelAdapter(_runtime_for_agent(runtime, definition), sub_registry)
        else:
            return ToolResult(
                ok=False,
                output="Task tool requires a configured model (no runtime available).",
            )

        # Register this sub-agent invocation on the trace tree so its token /
        # tool-call usage is visible to the parent session.
        trace_node = None
        if trace_manager is not None:
            trace_node = trace_manager.create(
                agent_type=definition.type.value,
                name=definition.name,
                parent_id=parent_trace_id,
            )

        # Forward sub-agent tool activity to the caller while also counting it
        # on the trace node.  Keeping these wrappers local avoids touching the
        # agent loop signature for bookkeeping that only the Task tool needs.
        def _wrap_tool_start(name: str, tool_input: dict) -> None:
            if trace_manager is not None and trace_node is not None:
                trace_manager.record_tool_call(trace_node.trace_id)
            if on_tool_start is not None:
                try:
                    on_tool_start(name, tool_input)
                except Exception:  # noqa: BLE001 -- callback must never break the loop
                    pass

        def _wrap_tool_result(name: str, output: str, ok: bool) -> None:
            if on_tool_result is not None:
                try:
                    on_tool_result(name, output, ok)
                except Exception:  # noqa: BLE001
                    pass

        def _record_trace_usage(usage: dict) -> None:
            if trace_manager is not None and trace_node is not None:
                trace_manager.record_tokens(
                    trace_node.trace_id,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )

        sub_messages: list[ChatMessage] = [
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
                cost_tracker=cost_tracker,
                on_usage=_record_trace_usage,
                on_tool_start=_wrap_tool_start,
                on_tool_result=_wrap_tool_result,
            )
        except Exception as error:  # noqa: BLE001
            if trace_manager is not None and trace_node is not None:
                trace_manager.complete(trace_node.trace_id, status="failed")
            return ToolResult(ok=False, output=f"Sub-agent ({definition.name}) failed: {error}")

        if trace_manager is not None and trace_node is not None:
            trace_manager.complete(trace_node.trace_id, status="completed")

        final = next(
            (m["content"] for m in reversed(result_messages) if m.get("role") == "assistant"),
            "",
        )
        tool_calls = sum(1 for m in result_messages if m.get("role") == "assistant_tool_call")
        summary = f"[Sub-agent {definition.name} completed | tool calls: {tool_calls}]"
        if trace_manager is not None and trace_node is not None:
            summary += " " + trace_manager.format_summary(trace_node.trace_id)
        summary += f"\n\n{final}"
        return ToolResult(ok=True, output=summary)

    return ToolDefinition(
        name="task",
        description=(
            "Delegate a scoped task to an isolated sub-agent that runs in its own "
            "context window and returns only a summary. agent_type: 'explore' "
            "(fast read-only search), 'plan' (thorough read-only analysis), "
            "'general' (full read/write/exec), or 'verification' (run builds, "
            "tests, and lint to verify a change). Use for broad codebase "
            "exploration or self-contained subtasks to keep the main context lean."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_type": {
                    "type": "string",
                    "description": "Built-in or project/user-defined markdown agent name",
                },
                "task": {"type": "string", "description": "Self-contained task description for the sub-agent"},
            },
            "required": ["task"],
        },
        validator=_validate,
        run=_run,
    )
