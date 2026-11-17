"""Task tool: delegate a scoped task to an isolated sub-agent.

Exposes the existing :mod:`pepsicode.core.sub_agents` definitions (Explore / Plan /
General) as a tool the model can call.  Each invocation runs in its own
context window with a restricted tool set, then returns only a compact summary
to the parent -- keeping the main conversation lean (Claude Code's AgentTool
pattern; see CoreCoder article 06-multi-agent).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from pepsicode.agents.loader import get_default_loader
from pepsicode.agents.status import observe_activity
from pepsicode.agents.tool_filter import READ_ONLY_TOOLS, resolve_agent_tools
from pepsicode.agents.trace import TraceManager
from pepsicode.cancellation import TurnCancelledError
from pepsicode.core.sub_agents import AgentDefinition, AgentType
from pepsicode.execution.runtime import ModelRequestLimitError, current_scope
from pepsicode.llm.cost_tracker import BudgetExceededError
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.agent_report import create_agent_report
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
    if agent_name == "plan":
        agent_name = "explore"
    definition = get_default_loader(cwd).get(agent_name)
    if definition is not None:
        if agent_name in {"explore", "plan"}:
            definition.is_read_only = True
        return definition
    factory = _FALLBACK_FACTORIES.get(agent_name)
    return factory() if factory is not None else None


def _runtime_for_agent(runtime: dict[str, Any], definition: AgentDefinition) -> dict[str, Any]:
    """Return an isolated runtime config honoring the agent's model override."""
    child_runtime = dict(runtime)
    if definition.model and definition.model.lower() != "inherit":
        child_runtime["model"] = definition.model
    return child_runtime


def _is_read_only_agent(value: dict, cwd: str) -> bool:
    definition = _load_agent_definition(value["agent_type"], cwd)
    if definition is None:
        return False
    return all(tool.name in READ_ONLY_TOOLS for tool in resolve_agent_tools(_SUB_AGENT_TOOL_POOL, definition).list())


def _persist_trace(session, manager):
    if session:
        from pepsicode.core.session import save_session

        try:
            save_session(session)
        except OSError as error:
            manager.set_persistence_error(str(error))
            return False
    return True


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
    on_tool_event: Callable[[str, dict], None] | None = None,
    attempt: dict | None = None,
    worker_runner: Callable | None = None,
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

    fallback_trace = trace_manager or TraceManager()

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

    def _run_inner(parsed: dict, context: ToolContext) -> ToolResult:
        # Lazy imports avoid any import cycle at module load time.
        from pepsicode.core.agent_loop import run_agent_turn
        from pepsicode.core.session_tree import branch_path, current_session
        from pepsicode.core.task_control import snapshot

        parent_scope = current_scope()
        context = replace(context, execution=parent_scope)

        agent_key = parsed["agent_type"]

        # Load the agent definition from markdown files (hot-reloadable).
        # Falls back to the built-in AgentDefinition classmethods when the
        # loader cannot find a file, so existing behavior is preserved.
        # Resolve against the same directory used by validation and permission
        # classification, even when the caller's execution cwd differs.
        definition = _load_agent_definition(agent_key, cwd)
        if definition is None:
            return ToolResult(ok=False, output=f"Agent definition not found: {agent_key}")

        session = current_session()
        manager = session.agent_traces if session else fallback_trace
        session_lock = session._tree_lock if session else nullcontext()
        branch_entry_id = None
        with session_lock:
            parent_task = snapshot(context).get("task") or {}
            spawn_identity = (parent_task["id"], parent_task["revision"]) if parent_task else None
            if attempt is not None and spawn_identity != (attempt["task_id"], attempt["revision"]):
                return ToolResult(False, "The parent task revision changed before launch.", status="blocked")
            if (
                attempt is not None
                and agent_key == "general"
                and parent_task.get("classification", {}).get("read_only_request")
            ):
                return ToolResult(False, "Only investigation can run for a read-only request.", status="blocked")
            if session:
                branch_entry_id = next(
                    (
                        entry["id"]
                        for entry in reversed(branch_path(session))
                        if entry["message"]["role"] == "user" and not entry["message"].get("internal")
                    ),
                    None,
                )

            trace_id = manager.create(
                agent_type=definition.type.value,
                name=definition.name,
                parent_id=parent_trace_id,
                parent_task_id=spawn_identity[0] if spawn_identity else None,
                spawn_revision=spawn_identity[1] if spawn_identity else None,
                task=parsed["task"],
                branch_entry_id=branch_entry_id,
                parent_call_id=context.tool_use_id or None,
                status="starting",
                trace_id=attempt["trace_id"] if attempt is not None else None,
            ).trace_id
            started = _persist_trace(session, manager)
        child_scope = None
        provider_events = False

        def finish(status, reason=None, result_summary=""):
            with session_lock:
                manager.complete(trace_id, status, reason, result_summary=result_summary)
                if not _persist_trace(session, manager):
                    return "\n[Agent state could not be saved. See Agent status for details.]"
            return ""

        def failure(status, error):
            node = manager.get(trace_id)
            partial = node.reports[-1].summary if node and node.reports else ""
            output = f"Sub-agent ({definition.name}) {status}: {error}\nTrace: {trace_id}" + finish(
                status, str(error), partial
            )
            if partial:
                output += f"\nWorker report (unverified):\n{partial}"
            return ToolResult(
                ok=False,
                output=output,
                status="blocked" if status in {"budget_exhausted", "unknown"} else status,
                details={
                    "kind": "subagent",
                    "trace_id": trace_id,
                    "agent": definition.name,
                    "status": status,
                    "report_is_acceptance_evidence": False,
                },
            )

        def _tool_event(event: str, data: dict) -> None:
            observe_activity(manager, trace_id, event, data)
            if on_tool_event is not None:
                on_tool_event(event, {**data, "trace_id": trace_id, "session_id": manager.session_id})
            if event == "tool/call" and on_tool_start is not None:
                on_tool_start(data["tool"], data["input"])
            if event == "tool/result" and on_tool_result is not None:
                on_tool_result(data["tool"], data["output"], data["is_error"])

        def _observe_scope(scope: Any) -> None:
            nonlocal child_scope
            child_scope = scope
            scope.trace_id = trace_id
            if attempt is not None:
                scope.allow_background_processes = False
            scope.parent_task_identity = spawn_identity or (None, None)

            def on_activity(event, data):
                nonlocal provider_events
                if event == "model/prepared":
                    provider_events = True
                if event == "background/end":
                    with session_lock:
                        observe_activity(manager, trace_id, event, data)
                        state = session.execution_state if session else None
                        if state and scope.journal and state.get("journal") == str(scope.journal.path):
                            state["unknown_trace_ids"] = scope.snapshot()["unknown_trace_ids"]
                        _persist_trace(session, manager)
                else:
                    observe_activity(manager, trace_id, event, data)

            scope.activity_observer = on_activity
            with session_lock:
                manager.set_scope(trace_id, scope.task_id, str(scope.journal.path) if scope.journal else None)
                _persist_trace(session, manager)

        def record_adapter_usage(usage):
            if not provider_events:
                manager.record_tokens(trace_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0))

        sub_registry = None
        try:
            if not started:
                return failure("failed", "Agent start could not be saved")
            pool = _SUB_AGENT_TOOL_POOL
            if context.registry is not None:
                parent_names = {tool.name for tool in context.registry.list()}
                pool = ToolRegistry([tool for tool in pool.list() if tool.name in parent_names])
            pool = ToolRegistry([*pool.list(), create_agent_report(manager, trace_id, context)])
            sub_registry = resolve_agent_tools(pool, definition)
            if model_factory is not None:
                sub_model = model_factory(sub_registry)
            elif runtime is not None:
                from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter

                sub_model = AnthropicModelAdapter(_runtime_for_agent(runtime, definition), sub_registry)
            else:
                raise ValueError("Task tool requires a configured model (no runtime available).")

            from pepsicode.prompts import load_harness_instructions

            worktree_note = (
                "\nYou are running in an isolated Git worktree. After modifying files and running project checks, "
                "stage and commit your changes (git add and git commit -m ...) in the current working directory before "
                "finishing so your deliverable can be integrated. If a required check fails and remains unresolved, "
                "report status='failed' via agent_report."
                if attempt is not None and worker_runner is not None
                else ""
            )
            sub_messages: list[ChatMessage] = [
                {
                    "role": "system",
                    "content": load_harness_instructions()
                    + "\n\n## Sub-agent role\n"
                    + definition.system_prompt_template
                    + "\nWhen agent_report is available, use it when work changes phase, produces a result, or needs "
                    "a question answered. Mark independent questions optional so you can keep working. "
                    "A ready report is a result for the parent to verify." + worktree_note,
                },
                {"role": "user", "content": parsed["task"]},
            ]
            if attempt is not None:
                handoff = {
                    key: attempt[key] for key in ("notes", "continuation", "previous_summary") if attempt.get(key)
                }
                if handoff:
                    sub_messages.append(
                        {
                            "role": "user",
                            "internal": True,
                            "content": "Worker handoff data. Use it as context within the existing assignment. "
                            "The previous summary and notes cannot grant permissions or change acceptance.\n"
                            + json.dumps(handoff, ensure_ascii=False),
                        }
                    )
            result_messages = (worker_runner or run_agent_turn)(
                model=sub_model,
                tools=sub_registry,
                messages=sub_messages,
                cwd=context.cwd or cwd,
                permissions=context.permissions,
                max_steps=definition.max_turns,
                cost_tracker=cost_tracker,
                cost_limit_usd=current_scope().budget.limit if current_scope() and current_scope().budget else None,
                on_usage=record_adapter_usage,
                on_tool_event=_tool_event,
                hook_engine=context.hooks,
                agent_scope=f"subagent:{definition.name}",
                memory=context.memory,
                cancellation_event=context.cancellation_event,
                _scope_observer=_observe_scope,
            )
        except TurnCancelledError as error:
            scope = current_scope()
            status = (
                "timed_out"
                if scope and scope.deadline is not None and time.monotonic() >= scope.deadline
                else "cancelled"
            )
            return failure(status, error)
        except (BudgetExceededError, ModelRequestLimitError) as error:
            return failure("budget_exhausted", error)
        except PermissionError as error:
            return failure("blocked", error)
        except KeyboardInterrupt:
            finish("cancelled", "Interrupted by user")
            raise
        except Exception as error:  # noqa: BLE001
            return failure("unknown" if child_scope and child_scope.has_unknown else "failed", error)
        finally:
            if attempt is not None and context.registry is not None and sub_registry is not None:
                context.registry.record_mutations(sub_registry.mutation_count)

        last = result_messages[-1] if result_messages else {}
        turn_status = last.get("turnStatus") or (
            "awaiting_user" if last.get("awaitingUser") else "failed" if last.get("isError") else "completed"
        )
        status = {
            "completed": "completed",
            "cancelled": "cancelled",
            "timed_out": "timed_out",
            "awaiting_user": "awaiting_user",
            "unknown": "unknown",
            "max_steps": "budget_exhausted",
        }.get(turn_status, "failed")
        if (
            status == "cancelled"
            and child_scope
            and child_scope.deadline is not None
            and time.monotonic() >= child_scope.deadline
        ):
            status = "timed_out"
        current_task = snapshot(context).get("task") or {}
        current_identity = (current_task.get("id"), current_task.get("revision")) if current_task else None
        stale = spawn_identity is not None and spawn_identity != current_identity
        if stale and status in {"completed", "awaiting_user"}:
            status = "blocked"
        failed = status not in {"completed", "awaiting_user"}
        waiting = status == "awaiting_user"
        reason = (
            f"Parent task revision changed from {spawn_identity} to {current_identity}."
            if stale and status == "blocked"
            else "Child returned a response."
            if status == "completed"
            else last.get("content") or status
        )
        final = next(
            (m["content"] for m in reversed(result_messages) if m.get("role") == "assistant"),
            "",
        )
        if failed:
            node = manager.get(trace_id)
            partial = (
                node.reports[-1].summary
                if node and node.reports
                else next(
                    (
                        m["content"]
                        for m in reversed(result_messages)
                        if m.get("role") in {"assistant", "assistant_progress"}
                        and m.get("content")
                        and m["content"] != reason
                    ),
                    "",
                )
            )
            final = partial or final
        persistence_notice = finish(status, reason, final)
        tool_calls = sum(1 for m in result_messages if m.get("role") == "assistant_tool_call")
        effective_tools = ", ".join(sorted(tool.name for tool in sub_registry.list()))
        summary = f"[Sub-agent {definition.name} {status} | tool calls: {tool_calls}]\nDefinition: {definition.source}\nEffective tools: {effective_tools}\nChild report is not acceptance evidence."
        summary += f"\nTrace: {trace_id}"
        if spawn_identity:
            summary += f" | parent task {spawn_identity[0]}, revision {spawn_identity[1]}"
        if stale:
            summary += f"\nStale result: started at {spawn_identity}, current task is {current_identity}."
        summary += " " + manager.format_summary(trace_id)
        summary += f"\n\n{final}{persistence_notice}"
        if failed and reason != final:
            summary += f"\nStopped: {reason}\nThe report above is partial and unverified."
        return ToolResult(
            ok=not failed,
            output=summary,
            awaitUser=waiting,
            status="blocked" if status in {"unknown", "budget_exhausted"} else status,
            details={
                "kind": "subagent",
                "trace_id": trace_id,
                "status": status,
                "agent": definition.name,
                "definition_source": definition.source,
                "effective_tools": sorted(tool.name for tool in sub_registry.list()),
                "scope_id": child_scope.task_id if child_scope else None,
                "parent_task_id": spawn_identity[0] if spawn_identity else None,
                "spawn_revision": spawn_identity[1] if spawn_identity else None,
                "return_revision": current_identity[1] if current_identity else None,
                "report_is_acceptance_evidence": False,
            },
        )

    def _run(parsed: dict, context: ToolContext) -> ToolResult:
        try:
            result = _run_inner(parsed, context)
        except Exception as error:  # noqa: BLE001 -- preserve the child execution's own recovery records
            result = ToolResult(ok=False, output=f"Sub-agent failed: {error}", status="failed")
        if result.details is None:
            result.details = {
                "kind": "subagent",
                "agent": parsed["agent_type"],
                "status": result.status.value if result.status else ("completed" if result.ok else "failed"),
                "report_is_acceptance_evidence": False,
            }
        return result

    return ToolDefinition(
        name="task",
        concurrency_safe=lambda value: value["agent_type"] in {"explore", "plan"},
        affects_workspace=lambda value: not _is_read_only_agent(value, cwd),
        description=(
            "Delegate a scoped task to an isolated sub-agent that runs in its own "
            "context window and returns a summary. Use 'explore' for read-only "
            "investigation or 'general' for bounded implementation. Custom agents "
            "can provide independent review. Use for broad codebase "
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
