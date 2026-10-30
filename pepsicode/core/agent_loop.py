from __future__ import annotations

import time
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from dataclasses import replace
from typing import Any, cast

from pepsicode.cancellation import TurnCancelledError, cancellable_turn, raise_if_cancelled
from pepsicode.context.context_artifacts import ContextArtifactStore, prepare_tool_result
from pepsicode.context.context_manager import ContextManager
from pepsicode.core.input_queue import QueuedInput, TurnInputQueue
from pepsicode.core.session_tree import checkpoint_messages
from pepsicode.execution.runtime import ModelRequestLimitError, current_scope, scoped_turn
from pepsicode.hooks import HookContext, HookEngine, HookEvent
from pepsicode.llm.anthropic_adapter import ContextOverflowError
from pepsicode.llm.assistant_text import AssistantTextStream, parse_assistant_text
from pepsicode.llm.cost_tracker import BudgetExceededError, CostTracker
from pepsicode.logging_config import get_logger
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry, ToolResult
from pepsicode.types import AgentStep, ChatMessage, ModelAdapter, StreamToken
from pepsicode.usage import prompt_tokens

logger = get_logger("agent_loop")

# Upper bound on concurrent read-only tool execution within a single batch.
MAX_PARALLEL_TOOLS = 8

# Constants: reusable nudge/prompt text
NUDGE_CONTINUE = (
    "Continue immediately from your <progress> update with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete."
)

NUDGE_AFTER_TOOL_RESULT = (
    "Continue from your progress update. You have already used tools in this turn, "
    "so treat plain status text as progress, not a final answer. Respond with the "
    "next concrete tool call, code change, or an explicit <final> answer only if "
    "the task is truly complete."
)

NUDGE_AFTER_EMPTY_RESPONSE = (
    "Your last response was empty after recent tool results. Continue immediately "
    "by trying the next concrete step, adapting to any tool errors, or giving an "
    "explicit <final> answer only if the task is complete."
)

NUDGE_AFTER_EMPTY_NO_TOOLS = (
    "Your last response was empty. Continue immediately with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete."
)

RESUME_AFTER_PAUSE = (
    "Resume from the previous pause and continue immediately with the next concrete "
    "tool call, code change, or an explicit <final> answer only if the task is complete."
)

RESUME_AFTER_MAX_TOKENS = (
    "Your previous response hit max_tokens during thinking before producing the next "
    "actionable step. Resume immediately and continue with the next concrete tool call, "
    "code change, or an explicit <final> answer only if the task is complete."
)


def _is_empty_assistant_response(content: str) -> bool:
    return len(content.strip()) == 0


def _format_diagnostics(
    stop_reason: str | None, block_types: list[str] | None, ignored_block_types: list[str] | None
) -> str:
    parts: list[str] = []
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")
    if block_types:
        parts.append(f"blocks={','.join(block_types)}")
    if ignored_block_types:
        parts.append(f"ignored={','.join(ignored_block_types)}")
    return f" Diagnostics: {'; '.join(parts)}." if parts else ""


def _is_recoverable_thinking_stop(
    *, is_empty: bool, stop_reason: str | None, block_types: list[str] | None, ignored_block_types: list[str] | None
) -> bool:
    if not is_empty:
        return False
    if stop_reason not in {"pause_turn", "max_tokens"}:
        return False
    return "thinking" in (block_types or []) or "thinking" in (ignored_block_types or [])


def _should_treat_assistant_as_progress(*, kind: str | None, content: str, saw_tool_result: bool) -> bool:
    if kind == "progress":
        return True
    if kind == "final":
        return False
    if not saw_tool_result:
        return False
    return False


def _unfinished_step(reason, text, messages, hooks, cwd, permissions, agent_scope, callback=None):
    if reason not in {"max_tokens", "stream_incomplete", "pause_turn", "refusal"}:
        return None
    message = (
        f"Model step stopped with {reason}; the task is incomplete and no tools from this response were dispatched."
    )
    if text:
        messages.append({"role": "assistant_progress", "content": text})
    messages.append({"role": "assistant", "content": message, "isError": True, "finishReason": reason})
    if callback:
        callback(message)
    return _finish_hook_turn(
        messages, hooks, cwd=cwd, permissions=permissions, agent_scope=agent_scope, status="error", error=message
    )


def _is_concurrency_safe(tools: ToolRegistry, tool_name: str) -> bool:
    tool = tools.find(tool_name)
    return bool(tool and getattr(tool, "concurrency_safe", False))


def _record_cost_usage(model: ModelAdapter, cost_tracker: CostTracker | None) -> None:
    """Feed the provider's reported token usage into the cost tracker.

    Mirrors the existing ``context_manager.update_usage`` recording: both read
    the same ``model.last_usage`` dict so context stats and cost stats stay in
    sync from a single source of truth.
    """
    if cost_tracker is None:
        return
    if getattr(model, "_usage_recorded", False):
        model._usage_recorded = False
        return
    usage = getattr(model, "last_usage", None)
    if not isinstance(usage, dict):
        return
    runtime = getattr(model, "runtime", None) or {}
    model_name = usage.get("model") or runtime.get("model") or getattr(model, "name", None) or "default"
    cost_tracker.add_usage(
        model=model_name,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_tokens", 0),
        cache_write_tokens=usage.get("cache_write_tokens", 0),
    )


def _notify_usage(model: ModelAdapter, on_usage: Callable[[dict], None] | None) -> None:
    """Forward one provider usage report to an optional observer."""
    if on_usage is None:
        return
    usage = getattr(model, "last_usage", None)
    if not isinstance(usage, dict):
        return
    try:
        on_usage(dict(usage))
    except Exception as error:  # noqa: BLE001 -- telemetry must not break the agent loop
        logger.debug("Usage callback failed: %s", error)


def _check_budget(cost_tracker: CostTracker | None, cost_limit_usd: float | None) -> None:
    """Raise ``BudgetExceededError`` if spend has crossed the cap, if any."""
    if cost_tracker is not None and cost_limit_usd is not None and cost_tracker.total_cost_usd >= cost_limit_usd:
        raise BudgetExceededError(limit=cost_limit_usd, spent=cost_tracker.total_cost_usd)


def _hook_metadata(permissions: PermissionManager | None, agent_scope: str, **extra: Any) -> dict[str, Any]:
    mode = getattr(permissions, "mode", "default")
    return {
        "permission_mode": getattr(mode, "value", str(mode)),
        "agent_scope": agent_scope,
        **extra,
    }


def _emit_hook(
    hook_engine: HookEngine | None,
    event: HookEvent,
    *,
    cwd: str,
    permissions: PermissionManager | None,
    agent_scope: str,
    data: dict[str, Any] | None = None,
) -> None:
    if hook_engine is None:
        return
    try:
        hook_engine.emit(
            event,
            HookContext(
                event=event,
                cwd=cwd,
                data=data or {},
                metadata=_hook_metadata(permissions, agent_scope),
            ),
        )
    except Exception as error:  # noqa: BLE001 - hooks must never break the agent loop
        logger.warning("Hook event %s failed: %s", event.value, error)


def _messages_for_model(
    messages: list[ChatMessage],
    hook_engine: HookEngine | None,
    *,
    cwd: str | None = None,
    permissions: PermissionManager | None = None,
    tools: ToolRegistry | None = None,
    memory: Any | None = None,
    runtime: dict | None = None,
) -> list[ChatMessage]:
    from pepsicode.core.task_control import task_context

    if messages and messages[0].get("_prompt_format") == 1 and cwd is not None and tools is not None:
        from pepsicode.context.prompt import build_session_prompt

        sections: list[str] = []
        build_session_prompt(
            cwd,
            permissions.get_summary() if permissions else [],
            skills=tools.get_skills(),
            mcp_servers=tools.get_mcp_servers(),
            memory_manager=memory,
            governance=bool((runtime or {}).get("governance")),
            plan_mode=permissions.is_plan_mode if permissions else False,
            plan_file_path=permissions.plan_file_path if permissions else None,
            dynamic_sections=sections,
        )
        session_text = (
            '<system-reminder kind="session-context">\n'
            "The latest session-context reminder replaces all earlier session-context reminders.\n"
            + "\n\n".join(sections)
            + "\n</system-reminder>"
        )
        prior_session = next(
            (
                m
                for m in reversed(messages)
                if isinstance(m.get("source"), dict) and m["source"].get("kind") == "session-context-projection"
            ),
            None,
        )
        prior_index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i] is prior_session), -1)
        new_human_input = any(m.get("role") == "user" and not m.get("internal") for m in messages[prior_index + 1 :])
        if prior_session is None or prior_session.get("content") != session_text or new_human_input:
            messages.append(
                cast(
                    ChatMessage,
                    {
                        "role": "user",
                        "internal": True,
                        "content": session_text,
                        "source": {"kind": "session-context-projection"},
                    },
                )
            )

    contract = task_context()
    previous = next(
        (
            m
            for m in reversed(messages)
            if isinstance(m.get("source"), dict) and m["source"].get("kind") == "task-policy-projection"
        ),
        None,
    )
    content = contract["content"] if contract else "No task contract is currently active."
    text = (
        '<system-reminder kind="task-contract">\n'
        "Host task state follows; the latest task-contract reminder supersedes earlier ones.\n"
        f"{content}\n</system-reminder>"
    )
    previous_index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i] is previous), -1)
    new_human_input = any(m.get("role") == "user" and not m.get("internal") for m in messages[previous_index + 1 :])
    if (contract or previous) and (previous is None or previous.get("content") != text or new_human_input):
        messages.append(
            cast(
                ChatMessage,
                {"role": "user", "internal": True, "content": text, "source": {"kind": "task-policy-projection"}},
            )
        )
    if hook_engine is not None:
        hook_context = hook_engine.drain_context_messages()
        if hook_context:
            messages.append(
                cast(
                    ChatMessage,
                    {
                        "role": "user",
                        "internal": True,
                        "content": '<system-reminder kind="hook-context">\n'
                        + "\n\n".join(hook_context)
                        + "\n</system-reminder>",
                        "source": {"kind": "hook-context"},
                    },
                )
            )
    checkpoint_messages(messages)
    return messages


def _finish_hook_turn(
    messages: list[ChatMessage],
    hook_engine: HookEngine | None,
    *,
    cwd: str,
    permissions: PermissionManager | None,
    agent_scope: str,
    status: str = "completed",
    error: str = "",
) -> list[ChatMessage]:
    if status in {"error", "max_steps"} and messages and messages[-1].get("role") == "assistant":
        messages[-1]["isError"] = True
        if error.startswith(("Network error", "Model API timeout")):
            messages[-1]["finishReason"] = "model_transport_failure"
    checkpoint_messages(messages)
    assistant_output = next(
        (str(message.get("content", "")) for message in reversed(messages) if message.get("role") == "assistant"),
        "",
    )
    if error:
        _emit_hook(
            hook_engine,
            HookEvent.ERROR,
            cwd=cwd,
            permissions=permissions,
            agent_scope=agent_scope,
            data={"error": error, "status": status},
        )
    if assistant_output:
        _emit_hook(
            hook_engine,
            HookEvent.ASSISTANT_OUTPUT,
            cwd=cwd,
            permissions=permissions,
            agent_scope=agent_scope,
            data={"assistant_output": assistant_output, "status": status},
        )
    stop_event = HookEvent.SUBAGENT_STOP if agent_scope.startswith("subagent") else HookEvent.AGENT_STOP
    _emit_hook(
        hook_engine,
        stop_event,
        cwd=cwd,
        permissions=permissions,
        agent_scope=agent_scope,
        data={"status": status, "error": error, "message_count": len(messages)},
    )
    return messages


def _finish_cancelled_turn(messages, hooks, cwd, permissions, scope, notify):
    message = "Task stopped by user."
    messages.append({"role": "assistant", "content": message})
    if notify:
        notify(message)
    return _finish_hook_turn(messages, hooks, cwd=cwd, permissions=permissions, agent_scope=scope, status="cancelled")


def append_queued_inputs(
    messages, pending: list[QueuedInput], *, cwd, permissions=None, hook_engine=None, agent_scope="main"
) -> bool:
    if not pending:
        return False
    import copy

    from pepsicode.core.task_control import admit_steering

    before = copy.deepcopy(messages)
    messages.extend(item.message() for item in pending)
    try:
        # Persist message delivery and intent in the same session commit before
        # hooks/model execution can observe a widened task contract.
        admit_steering(pending, messages=messages)
    except BaseException:
        messages[:] = before
        raise
    for item in pending:
        _emit_hook(
            hook_engine,
            HookEvent.USER_INPUT,
            cwd=cwd,
            permissions=permissions,
            agent_scope=agent_scope,
            data={
                "user_input": item.display_content or item.content,
                "expanded_input": item.content,
                "input_mode": item.mode,
            },
        )
    scope = current_scope()
    queue = getattr(scope, "input_queue", None)
    if queue is not None:
        queue.acknowledge(pending)
    return True


def _tool_recovery_nudge(calls, results):
    failed = [
        call["toolName"]
        for call, result in zip(calls, results, strict=False)
        if not result.ok and result.status not in {"blocked", "cancelled"} and not result.awaitUser
    ]
    if not failed:
        return None
    return {
        "role": "user",
        "internal": True,
        "content": (
            "A tool failed (" + ", ".join(failed) + "). Inspect the returned error and continue the user's task "
            "by correcting arguments, checking the current state, or choosing another appropriate tool. "
            "Do not blindly repeat a write or command: check whether it partially completed first. "
            "Do not bypass a permission denial or cancellation. If recovery requires user input, "
            "explain the concrete blocker and what is still incomplete; never claim the failed operation succeeded."
        ),
    }


def _execute_calls_in_order(
    calls: Sequence[dict[str, Any]],
    tools: ToolRegistry,
    context: ToolContext,
    on_tool_start: Callable[[str, dict[str, Any]], None] | None,
    on_tool_result: Callable[[str, str, bool], None] | None,
    on_tool_event: Callable[[str, dict[str, Any]], None] | None = None,
):
    """Keep model history ordered; structured observers see actual completion order."""

    def notify(name, data):
        if on_tool_event:
            try:
                on_tool_event(name, data)
            except Exception as error:  # noqa: BLE001 - observers cannot turn a finished tool into a failure
                logger.debug("Tool observer failed: %s", type(error).__name__)

    def execute(k, rejected=None):
        call = calls[k]
        started_at, clock = int(time.time() * 1000), time.monotonic()
        common = {"tool": call["toolName"], "call_id": str(call.get("id", "")), "started_at": started_at}
        notify("tool/call", {**common, "input": call["input"], "status": "running"})
        result = (
            rejected
            if rejected is not None
            else tools.execute(
                call["toolName"],
                call["input"],
                replace(context, tool_use_id=str(call.get("id", "")), call_index=k),
            )
        )
        duration = max(0, round((time.monotonic() - clock) * 1000))
        status = result.status or ("completed" if result.ok else "failed")
        call["_telemetry"] = {
            "startedAt": started_at,
            "durationMs": duration,
            "toolStatus": status,
            "exitCode": result.exit_code,
        }
        notify(
            "tool/result",
            {
                **common,
                "output": result.output,
                "is_error": not result.ok,
                "status": status,
                "duration_ms": duration,
                "exit_code": result.exit_code,
            },
        )
        return result

    def start(k):
        if on_tool_start and not on_tool_event:
            on_tool_start(calls[k]["toolName"], calls[k]["input"])

    def finish(k, result):
        if on_tool_result and not on_tool_event:
            on_tool_result(calls[k]["toolName"], result.output, not result.ok)

    results: list = [None] * len(calls)
    if len(calls) > 1 and any(call.get("toolName") == "exit_plan_mode" for call in calls):
        for k in range(len(calls)):
            start(k)
            results[k] = execute(
                k,
                ToolResult(
                    False,
                    "exit_plan_mode must be the only tool call in its response; no tools were executed.",
                    status="blocked",
                ),
            )
            finish(k, results[k])
        return results
    index = 0
    while index < len(calls):
        run, cursor = [], index
        while cursor < len(calls) and _is_concurrency_safe(tools, calls[cursor]["toolName"]):
            run.append(cursor)
            cursor += 1
        if len(run) > 1:
            for k in run:
                start(k)
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_TOOLS, len(run))) as pool:
                futures = {k: pool.submit(execute, k) for k in run}
                for k in run:
                    results[k] = futures[k].result()
                    finish(k, results[k])
            index = cursor
        else:
            start(index)
            results[index] = execute(index)
            finish(index, results[index])
            index += 1
    return results


@scoped_turn
@cancellable_turn
def run_agent_turn(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager | None = None,
    max_steps: int = 50,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str, bool], None] | None = None,
    on_tool_event: Callable[[str, dict[str, Any]], None] | None = None,
    on_assistant_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    context_manager: ContextManager | None = None,
    cost_tracker: CostTracker | None = None,
    cost_limit_usd: float | None = None,
    on_usage: Callable[[dict], None] | None = None,
    hook_engine: HookEngine | None = None,
    agent_scope: str = "main",
    memory: Any | None = None,
    cancellation_event: Any | None = None,
    input_queue: TurnInputQueue | None = None,
) -> list[ChatMessage]:
    current_messages = list(messages)
    checkpoint_messages(current_messages)
    saw_tool_result = False
    empty_response_retry_count = 0
    recoverable_thinking_retry_count = 0
    overflow_retry_count = 0
    tool_error_count = 0
    step = 0
    artifact_store = ContextArtifactStore.for_workspace(cwd)
    start_event = HookEvent.SUBAGENT_START if agent_scope.startswith("subagent") else HookEvent.AGENT_START
    _emit_hook(
        hook_engine,
        start_event,
        cwd=cwd,
        permissions=permissions,
        agent_scope=agent_scope,
        data={"message_count": len(current_messages)},
    )

    if context_manager:
        context_manager.update_runtime_state(cwd, permissions)
        context_manager.messages = cast(list[dict[str, Any]], current_messages)
        stats = context_manager.get_stats()
        logger.info(
            "Context: %d tokens (%.0f%%), %d messages", stats.total_tokens, stats.usage_percentage, stats.messages_count
        )

        # Auto-compact if usage is near the limit
        if context_manager.should_auto_compact():
            logger.warning("Context near limit, auto-compacting...")
            previous_count = len(current_messages)
            current_messages = cast(list[ChatMessage], context_manager.compact_messages())
            _emit_hook(
                hook_engine,
                HookEvent.CONTEXT_COMPACT,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                data={"reason": "automatic", "before": previous_count, "after": len(current_messages)},
            )
            if on_assistant_message:
                on_assistant_message(context_manager.get_context_summary())

    while max_steps is None or step < max_steps:
        step += 1
        checkpoint_messages(current_messages)
        # Budget guard: stop before doing more work once the cap is hit.
        _check_budget(cost_tracker, cost_limit_usd)
        if input_queue is not None and not (cancellation_event is not None and cancellation_event.is_set()):
            append_queued_inputs(
                current_messages,
                input_queue.take_steering(),
                cwd=cwd,
                permissions=permissions,
                hook_engine=hook_engine,
                agent_scope=agent_scope,
            )
        next_step: AgentStep
        if context_manager is not None:
            context_manager.messages = cast(list[dict[str, Any]], current_messages)
            if context_manager.should_auto_compact():
                before = len(current_messages)
                current_messages = cast(list[ChatMessage], context_manager.compact_messages())
                if context_manager.last_compaction_changed:
                    _emit_hook(
                        hook_engine,
                        HookEvent.CONTEXT_COMPACT,
                        cwd=cwd,
                        permissions=permissions,
                        agent_scope=agent_scope,
                        data={"reason": "automatic", "before": before, "after": len(current_messages)},
                    )
        try:
            raise_if_cancelled()
            next_step = model.next(
                _messages_for_model(
                    current_messages,
                    hook_engine,
                    cwd=cwd,
                    permissions=permissions,
                    tools=tools,
                    memory=memory,
                    runtime=getattr(model, "runtime", None),
                )
            )
            raise_if_cancelled()
        except TurnCancelledError:
            return _finish_cancelled_turn(
                current_messages, hook_engine, cwd, permissions, agent_scope, on_assistant_message
            )
        except (KeyboardInterrupt, BudgetExceededError, ModelRequestLimitError):
            raise  # Let Ctrl-C propagate
        except ContextOverflowError as error:
            # API rejected the request as too large.  Compact and retry rather
            # than failing the turn (mirrors Claude Code's 400/413 handling).
            if context_manager is not None and overflow_retry_count < 3:
                overflow_retry_count += 1
                logger.warning(
                    "Context overflow (%s); compacting and retrying (attempt %d)", error, overflow_retry_count
                )
                context_manager.messages = cast(list[dict[str, Any]], current_messages)
                previous_count = len(current_messages)
                current_messages = cast(list[ChatMessage], context_manager.compact_messages(force=True))
                _emit_hook(
                    hook_engine,
                    HookEvent.CONTEXT_COMPACT,
                    cwd=cwd,
                    permissions=permissions,
                    agent_scope=agent_scope,
                    data={"reason": "overflow", "before": previous_count, "after": len(current_messages)},
                )
                if not context_manager.last_compaction_changed:
                    if overflow_retry_count == 1:
                        logger.warning("Compaction made no change; allowing one direct retry before circuit break")
                        step -= 1
                        continue
                    reason = context_manager.last_compaction_error or "no safe reduction was available"
                    fallback = f"Context too large and safe compaction made no progress ({reason}): {error}"
                    logger.error("Context overflow, compaction circuit stopped retries: %s", reason)
                    if on_assistant_message:
                        on_assistant_message(fallback)
                    current_messages.append({"role": "assistant", "content": fallback})
                    return _finish_hook_turn(
                        current_messages,
                        hook_engine,
                        cwd=cwd,
                        permissions=permissions,
                        agent_scope=agent_scope,
                        status="error",
                        error=fallback,
                    )
                if on_progress_message:
                    on_progress_message(context_manager.get_context_summary())
                step -= 1  # don't consume a step on a pure retry
                continue
            fallback = f"Context too large and could not be compacted further: {error}"
            logger.error("Context overflow, no recovery: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )
        except ConnectionError as error:
            fallback = f"Network error (connection failed or dropped): {error}"
            logger.error("Model API connection error: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )
        except TimeoutError as error:
            fallback = f"Model API timeout: {error}"
            logger.error("Model API timeout: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )
        except Exception as error:
            # Catch-all for unexpected errors (rate limit, auth, server 5xx, etc.)
            error_type = type(error).__name__
            fallback = f"Model API error ({error_type}): {error}"
            logger.error("Model API error (%s): %s", error_type, error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )

        # Record real token usage from the provider when available, so context
        # stats reflect what the model actually saw rather than an estimate.
        if context_manager is not None:
            usage = getattr(model, "last_usage", None)
            if isinstance(usage, dict):
                context_manager.update_usage(
                    prompt_tokens(usage),
                    usage.get("output_tokens", 0),
                )
        _record_cost_usage(model, cost_tracker)
        _notify_usage(model, on_usage)

        diagnostics = next_step.diagnostics
        reason = diagnostics.stopReason if diagnostics else None
        recoverable_empty_thinking = _is_recoverable_thinking_stop(
            is_empty=not next_step.content.strip() and not next_step.calls,
            stop_reason=reason,
            block_types=diagnostics.blockTypes if diagnostics else None,
            ignored_block_types=diagnostics.ignoredBlockTypes if diagnostics else None,
        )
        unfinished = (
            None
            if recoverable_empty_thinking
            else _unfinished_step(
                reason,
                next_step.content,
                current_messages,
                hook_engine,
                cwd,
                permissions,
                agent_scope,
                on_assistant_message,
            )
        )
        if unfinished is not None:
            return unfinished
        if next_step.type == "assistant":
            is_empty = _is_empty_assistant_response(next_step.content)
            if not is_empty and _should_treat_assistant_as_progress(
                kind=getattr(next_step, "kind", None),
                content=next_step.content,
                saw_tool_result=saw_tool_result,
            ):
                # Preserve thinking blocks before progress message
                thinking_blocks = getattr(next_step, "thinkingBlocks", None)
                if thinking_blocks:
                    current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
                if on_progress_message:
                    on_progress_message(next_step.content)
                current_messages.append({"role": "assistant_progress", "content": next_step.content})
                current_messages.append(
                    {
                        "role": "user",
                        "internal": True,
                        "content": (
                            NUDGE_AFTER_TOOL_RESULT
                            if saw_tool_result and getattr(next_step, "kind", None) != "progress"
                            else NUDGE_CONTINUE
                        ),
                    }
                )
                continue

            diagnostics = next_step.diagnostics

            if (
                _is_recoverable_thinking_stop(
                    is_empty=is_empty,
                    stop_reason=diagnostics.stopReason if diagnostics else None,
                    block_types=diagnostics.blockTypes if diagnostics else None,
                    ignored_block_types=diagnostics.ignoredBlockTypes if diagnostics else None,
                )
                and recoverable_thinking_retry_count < 3
            ):
                recoverable_thinking_retry_count += 1
                stop_reason = diagnostics.stopReason if diagnostics else None
                # Preserve thinking blocks from the interrupted response
                thinking_blocks = getattr(next_step, "thinkingBlocks", None)
                if thinking_blocks:
                    current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
                progress_content = (
                    "Model hit max_tokens during thinking; requesting the next step."
                    if stop_reason == "max_tokens"
                    else "Model returned pause_turn; requesting the next step."
                )
                if on_progress_message:
                    on_progress_message(progress_content)
                current_messages.append({"role": "assistant_progress", "content": progress_content})
                current_messages.append(
                    {
                        "role": "user",
                        "internal": True,
                        "content": (RESUME_AFTER_PAUSE if stop_reason == "pause_turn" else RESUME_AFTER_MAX_TOKENS),
                    }
                )
                continue

            if is_empty and empty_response_retry_count < 2:
                empty_response_retry_count += 1
                current_messages.append(
                    {
                        "role": "user",
                        "internal": True,
                        "content": (NUDGE_AFTER_EMPTY_RESPONSE if saw_tool_result else NUDGE_AFTER_EMPTY_NO_TOOLS),
                    }
                )
                continue

            if is_empty:
                diagnostics_suffix = _format_diagnostics(
                    diagnostics.stopReason if diagnostics else None,
                    diagnostics.blockTypes if diagnostics else None,
                    diagnostics.ignoredBlockTypes if diagnostics else None,
                )
                if saw_tool_result:
                    fallback = (
                        f"Model returned an empty response after tool execution and the turn was stopped. There were {tool_error_count} tool error(s); retry, adjust the command, or choose a different approach.{diagnostics_suffix}"
                        if tool_error_count > 0
                        else f"Model returned an empty response after tool execution and the turn was stopped. Retry or ask the model to continue the remaining steps.{diagnostics_suffix}"
                    )
                else:
                    fallback = f"Model returned an empty response and the turn was stopped.{diagnostics_suffix}"
                if on_assistant_message:
                    on_assistant_message(fallback)
                # Preserve thinking blocks even on empty response
                thinking_blocks = getattr(next_step, "thinkingBlocks", None)
                if thinking_blocks:
                    current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
                current_messages.append({"role": "assistant", "content": fallback})
                return _finish_hook_turn(
                    current_messages,
                    hook_engine,
                    cwd=cwd,
                    permissions=permissions,
                    agent_scope=agent_scope,
                    status="error",
                    error=fallback,
                )

            # Preserve thinking blocks before final assistant response
            thinking_blocks = getattr(next_step, "thinkingBlocks", None)
            if thinking_blocks:
                current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
            if on_assistant_message:
                on_assistant_message(next_step.content)
            current_messages.append({"role": "assistant", "content": next_step.content})
            if input_queue is not None and not (cancellation_event is not None and cancellation_event.is_set()):
                if append_queued_inputs(
                    current_messages,
                    input_queue.take_steering(),
                    cwd=cwd,
                    permissions=permissions,
                    hook_engine=hook_engine,
                    agent_scope=agent_scope,
                ):
                    continue
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
            )

        # Preserve thinking blocks before tool call messages (always, even without content)
        thinking_blocks = getattr(next_step, "thinkingBlocks", None)
        if thinking_blocks:
            current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})

        if next_step.content:
            role = "assistant_progress" if next_step.contentKind == "progress" else "assistant"
            if role == "assistant_progress":
                if on_progress_message:
                    on_progress_message(next_step.content)
                current_messages.append({"role": role, "content": next_step.content})
                if not next_step.calls:
                    current_messages.append(
                        {
                            "role": "user",
                            "internal": True,
                            "content": NUDGE_CONTINUE,
                        }
                    )
            else:
                if on_assistant_message:
                    on_assistant_message(next_step.content)
                current_messages.append({"role": role, "content": next_step.content})

        if not next_step.calls and next_step.content and next_step.contentKind != "progress":
            if input_queue is not None and not (cancellation_event is not None and cancellation_event.is_set()):
                if append_queued_inputs(
                    current_messages,
                    input_queue.take_steering(),
                    cwd=cwd,
                    permissions=permissions,
                    hook_engine=hook_engine,
                    agent_scope=agent_scope,
                ):
                    continue
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
            )

        for call in next_step.calls:
            current_messages.append(
                {
                    "role": "assistant_tool_call",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "input": call["input"],
                }
            )

        context = ToolContext(
            cwd=cwd,
            execution=current_scope(),
            permissions=permissions,
            hooks=hook_engine,
            agent_scope=agent_scope,
            memory=memory,
            cancellation_event=cancellation_event,
        )
        results = _execute_calls_in_order(
            cast(Sequence[dict[str, Any]], next_step.calls),
            tools,
            context,
            on_tool_start,
            on_tool_result,
            on_tool_event,
        )

        await_user_result = None
        for call, result in zip(next_step.calls, results, strict=False):
            saw_tool_result = True
            if not result.ok:
                tool_error_count += 1
            tool = tools.find(call["toolName"])
            bounded_output = prepare_tool_result(
                tool_name=call["toolName"],
                output=result.output,
                budget_chars=tool.max_result_size_chars if tool is not None else 4_000,
                artifact_store=artifact_store,
            )
            current_messages.append(
                {
                    "role": "tool_result",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "content": bounded_output,
                    "isError": not result.ok,
                    **call.get("_telemetry", {}),
                }
            )
            if context_manager is not None:
                context_manager.observe_tool_result(
                    call["toolName"],
                    call.get("input") or {},
                    bounded_output,
                    result.ok,
                )
            if result.awaitUser and await_user_result is None:
                await_user_result = result

        if await_user_result is None:
            guidance = _tool_recovery_nudge(next_step.calls, results)
            if guidance:
                current_messages.append(guidance)
        if await_user_result is not None:
            if on_assistant_message:
                on_assistant_message(await_user_result.output)
            current_messages.append({"role": "assistant", "content": await_user_result.output, "awaitingUser": True})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="awaiting_user",
            )

    fallback = "Reached the maximum tool step limit for this turn."
    if on_assistant_message:
        on_assistant_message(fallback)
    current_messages.append({"role": "assistant", "content": fallback})
    return _finish_hook_turn(
        current_messages,
        hook_engine,
        cwd=cwd,
        permissions=permissions,
        agent_scope=agent_scope,
        status="max_steps",
        error=fallback,
    )


# ---------------------------------------------------------------------------
# Streaming agent turn
# ---------------------------------------------------------------------------


def _accumulate_stream_tokens(
    token_stream: Generator[StreamToken, None, None],
    *,
    on_token: Callable[[StreamToken], None] | None = None,
    thinking_blocks: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Consume the token stream and accumulate text + tool-call blocks.

    Returns ``(text_content, tool_calls | None)``.  When the response contains
    only plain text, ``tool_calls`` is ``None``.  When a tool-use block is
    present, the list contains dicts with keys ``id``, ``toolName``, and
    ``input`` (the fully-assembled JSON string, parsed to a dict).
    Completed thinking blocks are appended to ``thinking_blocks`` when given.

    The function keeps the tool-calling state-machine minimal:
    1. ``content_block_start(type=tool_use)``  -> enter tool mode
    2. ``input_json_delta``                    -> accumulate JSON fragments
    3. ``content_block_start(type=...)``       -> close current tool, start next block
    4. ``done``                                -> finalise
    """
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    # Current tool being accumulated (None until a tool_use block starts).
    current_tool: dict | None = None
    current_input_json: str = ""
    visible_text = AssistantTextStream()

    def flush_visible_text() -> None:
        tail = visible_text.finish()
        if tail and on_token is not None:
            on_token(StreamToken(type="text", content=tail))

    for token in token_stream:
        if token.type == "done":
            flush_visible_text()
        if token.type == "text":
            delta = visible_text.feed(token.content)
            if delta and on_token is not None:
                on_token(replace(token, content=delta))
        elif on_token is not None:
            on_token(token)

        if token.type == "text":
            text_parts.append(token.content)

        elif token.type == "thinking":
            if token.thinking_block is not None and thinking_blocks is not None:
                thinking_blocks.append(token.thinking_block)

        elif token.type == "tool_use":
            # A tool_use token with tool_name set marks the *start* of a new block.
            if token.tool_name is not None:
                # Finalise the previous tool if one was being accumulated.
                if current_tool is not None and current_input_json:
                    try:
                        import json as _json

                        current_tool["input"] = _json.loads(current_input_json)
                    except Exception:  # noqa: BLE001
                        current_tool["input"] = current_input_json
                    tool_calls.append(current_tool)
                current_tool = {
                    "id": token.tool_id or "",
                    "toolName": token.tool_name,
                    "input": None,
                }
                current_input_json = ""
            # Incremental input JSON fragment.
            if token.tool_input_partial:
                current_input_json += token.tool_input_partial

        elif token.type == "done":
            break

    flush_visible_text()

    # Flush the last accumulated tool call.
    if current_tool is not None:
        if current_input_json:
            try:
                import json as _json

                current_tool["input"] = _json.loads(current_input_json)
            except Exception:  # noqa: BLE001
                current_tool["input"] = current_input_json
        tool_calls.append(current_tool)

    text = "".join(text_parts)
    return text, tool_calls if tool_calls else None


@scoped_turn
@cancellable_turn
def run_agent_turn_stream(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager | None = None,
    max_steps: int = 50,
    on_token: Callable[[StreamToken], None] | None = None,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str, bool], None] | None = None,
    on_tool_event: Callable[[str, dict[str, Any]], None] | None = None,
    on_assistant_message: Callable[[str], None] | None = None,
    on_error_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    context_manager: ContextManager | None = None,
    cost_tracker: CostTracker | None = None,
    cost_limit_usd: float | None = None,
    on_usage: Callable[[dict], None] | None = None,
    hook_engine: HookEngine | None = None,
    agent_scope: str = "main",
    memory: Any | None = None,
    cancellation_event: Any | None = None,
    input_queue: TurnInputQueue | None = None,
) -> list[ChatMessage]:
    """Run an agent turn with streaming token output.

    This is the streaming counterpart of ``run_agent_turn``.  Instead of
    waiting for the full response, it receives ``StreamToken`` objects as they
    arrive from the provider via ``model.next_stream()`` and forwards them to
    the optional ``on_token`` callback, enabling real-time UI updates.

    All other semantics (tool execution, context compression, retries) are
    identical to the non-streaming variant.
    """
    current_messages = list(messages)
    checkpoint_messages(current_messages)
    saw_tool_result = False
    empty_response_retry_count = 0
    overflow_retry_count = 0
    tool_error_count = 0
    step = 0

    def emit_error(message: str) -> None:
        if on_error_message is not None:
            on_error_message(message)
        elif on_assistant_message is not None:
            on_assistant_message(message)

    artifact_store = ContextArtifactStore.for_workspace(cwd)
    start_event = HookEvent.SUBAGENT_START if agent_scope.startswith("subagent") else HookEvent.AGENT_START
    _emit_hook(
        hook_engine,
        start_event,
        cwd=cwd,
        permissions=permissions,
        agent_scope=agent_scope,
        data={"message_count": len(current_messages)},
    )

    if context_manager:
        context_manager.update_runtime_state(cwd, permissions)
        context_manager.messages = cast(list[dict[str, Any]], current_messages)
        stats = context_manager.get_stats()
        logger.info(
            "Context: %d tokens (%.0f%%), %d messages",
            stats.total_tokens,
            stats.usage_percentage,
            stats.messages_count,
        )
        if context_manager.should_auto_compact():
            logger.warning("Context near limit, auto-compacting...")
            previous_count = len(current_messages)
            current_messages = cast(list[ChatMessage], context_manager.compact_messages())
            _emit_hook(
                hook_engine,
                HookEvent.CONTEXT_COMPACT,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                data={"reason": "automatic", "before": previous_count, "after": len(current_messages)},
            )
            if on_assistant_message:
                on_assistant_message(context_manager.get_context_summary())

    while max_steps is None or step < max_steps:
        step += 1
        checkpoint_messages(current_messages)
        # Budget guard: stop before doing more work once the cap is hit.
        _check_budget(cost_tracker, cost_limit_usd)
        if input_queue is not None and not (cancellation_event is not None and cancellation_event.is_set()):
            append_queued_inputs(
                current_messages,
                input_queue.take_steering(),
                cwd=cwd,
                permissions=permissions,
                hook_engine=hook_engine,
                agent_scope=agent_scope,
            )

        # ------ Stream the model response ------
        if context_manager is not None:
            context_manager.messages = cast(list[dict[str, Any]], current_messages)
            if context_manager.should_auto_compact():
                before = len(current_messages)
                current_messages = cast(list[ChatMessage], context_manager.compact_messages())
                if context_manager.last_compaction_changed:
                    _emit_hook(
                        hook_engine,
                        HookEvent.CONTEXT_COMPACT,
                        cwd=cwd,
                        permissions=permissions,
                        agent_scope=agent_scope,
                        data={"reason": "automatic", "before": before, "after": len(current_messages)},
                    )
        text_content: str = ""
        parsed_calls: list[dict] | None = None
        thinking_blocks: list[dict[str, Any]] = []
        stream_stop_reason = None
        try:
            raise_if_cancelled()
            # For the first step we use next_stream(); retries of the same step
            # fall back to the non-streaming path so error handling stays simple.
            if step == 1 or True:  # always stream for now

                def on_stream_token(token: StreamToken) -> None:
                    nonlocal stream_stop_reason
                    raise_if_cancelled()
                    if token.type == "done":
                        stream_stop_reason = token.stop_reason
                    if token.type == "retry":
                        record = {
                            "role": "assistant_progress",
                            "content": token.content,
                            "activityKind": "retry",
                            "retry": token.retry,
                        }
                        current_messages.append(record)
                        if on_tool_event:
                            on_tool_event("request/retry", {"content": token.content, "retry": token.retry})
                        elif on_progress_message:
                            on_progress_message(token.content)
                        return
                    if token.type == "usage" and token.usage is not None and context_manager is not None:
                        context_manager.update_usage(prompt_tokens(token.usage), token.usage.get("output_tokens", 0))
                    if on_token is not None:
                        on_token(token)

                token_stream = model.next_stream(
                    _messages_for_model(
                        current_messages,
                        hook_engine,
                        cwd=cwd,
                        permissions=permissions,
                        tools=tools,
                        memory=memory,
                        runtime=getattr(model, "runtime", None),
                    )
                )
                with closing(token_stream) if hasattr(token_stream, "close") else nullcontext(token_stream):
                    text_content, parsed_calls = _accumulate_stream_tokens(
                        token_stream,
                        on_token=on_stream_token,
                        thinking_blocks=thinking_blocks,
                    )
            else:
                # Fallback non-stream (kept as reference; can be removed later)
                next_step = model.next(
                    _messages_for_model(
                        current_messages,
                        hook_engine,
                        cwd=cwd,
                        permissions=permissions,
                        tools=tools,
                        memory=memory,
                        runtime=getattr(model, "runtime", None),
                    )
                )
                text_content = next_step.content
                parsed_calls = (
                    [{"id": c["id"], "toolName": c["toolName"], "input": c.get("input")} for c in next_step.calls]
                    if next_step.calls
                    else None
                )
            raise_if_cancelled()

        except TurnCancelledError:
            return _finish_cancelled_turn(
                current_messages, hook_engine, cwd, permissions, agent_scope, on_progress_message
            )
        except (KeyboardInterrupt, BudgetExceededError, ModelRequestLimitError):
            raise
        except ContextOverflowError as error:
            if context_manager is not None and overflow_retry_count < 3:
                overflow_retry_count += 1
                logger.warning(
                    "Context overflow (%s); compacting and retrying (attempt %d)",
                    error,
                    overflow_retry_count,
                )
                context_manager.messages = cast(list[dict[str, Any]], current_messages)
                previous_count = len(current_messages)
                current_messages = cast(list[ChatMessage], context_manager.compact_messages(force=True))
                _emit_hook(
                    hook_engine,
                    HookEvent.CONTEXT_COMPACT,
                    cwd=cwd,
                    permissions=permissions,
                    agent_scope=agent_scope,
                    data={"reason": "overflow", "before": previous_count, "after": len(current_messages)},
                )
                if not context_manager.last_compaction_changed:
                    if overflow_retry_count == 1:
                        logger.warning("Compaction made no change; allowing one direct retry before circuit break")
                        step -= 1
                        continue
                    reason = context_manager.last_compaction_error or "no safe reduction was available"
                    fallback = f"Context too large and safe compaction made no progress ({reason}): {error}"
                    logger.error("Context overflow, compaction circuit stopped retries: %s", reason)
                    emit_error(fallback)
                    current_messages.append({"role": "assistant", "content": fallback, "isError": True})
                    return _finish_hook_turn(
                        current_messages,
                        hook_engine,
                        cwd=cwd,
                        permissions=permissions,
                        agent_scope=agent_scope,
                        status="error",
                        error=fallback,
                    )
                if on_progress_message:
                    on_progress_message(context_manager.get_context_summary())
                step -= 1
                continue
            fallback = f"Context too large and could not be compacted further: {error}"
            logger.error("Context overflow, no recovery: %s", error)
            emit_error(fallback)
            current_messages.append({"role": "assistant", "content": fallback, "isError": True})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )

        except ConnectionError as error:
            fallback = f"Network error (connection failed or dropped): {error}"
            logger.error("Model API connection error: %s", error)
            emit_error(fallback)
            current_messages.append({"role": "assistant", "content": fallback, "isError": True})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )

        except TimeoutError as error:
            fallback = f"Model API timeout: {error}"
            logger.error("Model API timeout: %s", error)
            emit_error(fallback)
            current_messages.append({"role": "assistant", "content": fallback, "isError": True})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )

        except Exception as error:
            error_type = type(error).__name__
            fallback = f"Model API error ({error_type}): {error}"
            logger.error("Model API error (%s): %s", error_type, error)
            emit_error(fallback)
            current_messages.append({"role": "assistant", "content": fallback, "isError": True})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )

        # ------ Record token usage ------
        if context_manager is not None:
            usage = getattr(model, "last_usage", None)
            if isinstance(usage, dict):
                context_manager.update_usage(
                    prompt_tokens(usage),
                    usage.get("output_tokens", 0),
                )
        _record_cost_usage(model, cost_tracker)
        _notify_usage(model, on_usage)

        unfinished = _unfinished_step(
            stream_stop_reason, text_content, current_messages, hook_engine, cwd, permissions, agent_scope, emit_error
        )
        if unfinished is not None:
            return unfinished
        text_content, text_kind = parse_assistant_text(text_content)

        # ------ Handle empty responses ------
        is_empty = len(text_content.strip()) == 0 and not parsed_calls

        if is_empty and empty_response_retry_count < 2:
            empty_response_retry_count += 1
            current_messages.append(
                {
                    "role": "user",
                    "internal": True,
                    "content": (NUDGE_AFTER_EMPTY_RESPONSE if saw_tool_result else NUDGE_AFTER_EMPTY_NO_TOOLS),
                }
            )
            continue

        if is_empty:
            fallback = "Model returned an empty response and the turn was stopped."
            emit_error(fallback)
            current_messages.append({"role": "assistant", "content": fallback, "isError": True})
            return _finish_hook_turn(
                current_messages,
                hook_engine,
                cwd=cwd,
                permissions=permissions,
                agent_scope=agent_scope,
                status="error",
                error=fallback,
            )

        # ------ Handle tool calls ------
        if parsed_calls:
            # Signed thinking must precede the tool calls it led to, or the API rejects the replay.
            if thinking_blocks:
                current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
            # Emit the assistant text as progress (if any) before tool calls.
            if text_content.strip():
                if on_progress_message:
                    on_progress_message(text_content)
                current_messages.append({"role": "assistant_progress", "content": text_content})

            # Record the tool-call messages.
            for call in parsed_calls:
                current_messages.append(
                    {
                        "role": "assistant_tool_call",
                        "toolUseId": call["id"],
                        "toolName": call["toolName"],
                        "input": call.get("input"),
                    }
                )

            context = ToolContext(
                cwd=cwd,
                execution=current_scope(),
                permissions=permissions,
                hooks=hook_engine,
                agent_scope=agent_scope,
                memory=memory,
                cancellation_event=cancellation_event,
            )
            results = _execute_calls_in_order(
                cast(Sequence[dict[str, Any]], parsed_calls),
                tools,
                context,
                on_tool_start,
                on_tool_result,
                on_tool_event,
            )

            await_user_result = None
            for call, result in zip(parsed_calls, results, strict=False):
                saw_tool_result = True
                if not result.ok:
                    tool_error_count += 1
                tool = tools.find(call["toolName"])
                bounded_output = prepare_tool_result(
                    tool_name=call["toolName"],
                    output=result.output,
                    budget_chars=tool.max_result_size_chars if tool is not None else 4_000,
                    artifact_store=artifact_store,
                )
                current_messages.append(
                    {
                        "role": "tool_result",
                        "toolUseId": call["id"],
                        "toolName": call["toolName"],
                        "content": bounded_output,
                        "isError": not result.ok,
                        **call.get("_telemetry", {}),
                    }
                )
                if context_manager is not None:
                    context_manager.observe_tool_result(
                        call["toolName"],
                        call.get("input") or {},
                        bounded_output,
                        result.ok,
                    )
                if result.awaitUser and await_user_result is None:
                    await_user_result = result

            if await_user_result is None:
                guidance = _tool_recovery_nudge(parsed_calls, results)
                if guidance:
                    current_messages.append(guidance)
            if await_user_result is not None:
                if on_assistant_message:
                    on_assistant_message(await_user_result.output)
                current_messages.append(
                    {"role": "assistant", "content": await_user_result.output, "awaitingUser": True}
                )
                return _finish_hook_turn(
                    current_messages,
                    hook_engine,
                    cwd=cwd,
                    permissions=permissions,
                    agent_scope=agent_scope,
                    status="awaiting_user",
                )

            # Continue to the next agent step after tool execution.
            continue

        if thinking_blocks:
            current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})

        if text_kind == "progress":
            if on_progress_message:
                on_progress_message(text_content)
            current_messages.append({"role": "assistant_progress", "content": text_content})
            current_messages.append({"role": "user", "content": NUDGE_CONTINUE, "internal": True})
            continue

        # ------ Plain text final answer ------
        if on_assistant_message:
            on_assistant_message(text_content)
        current_messages.append({"role": "assistant", "content": text_content})
        if input_queue is not None and not (cancellation_event is not None and cancellation_event.is_set()):
            if append_queued_inputs(
                current_messages,
                input_queue.take_steering(),
                cwd=cwd,
                permissions=permissions,
                hook_engine=hook_engine,
                agent_scope=agent_scope,
            ):
                continue
        return _finish_hook_turn(
            current_messages,
            hook_engine,
            cwd=cwd,
            permissions=permissions,
            agent_scope=agent_scope,
        )

    fallback = "Reached the maximum tool step limit for this turn."
    emit_error(fallback)
    current_messages.append({"role": "assistant", "content": fallback, "isError": True})
    return _finish_hook_turn(
        current_messages,
        hook_engine,
        cwd=cwd,
        permissions=permissions,
        agent_scope=agent_scope,
        status="max_steps",
        error=fallback,
    )
