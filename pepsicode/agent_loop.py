from __future__ import annotations

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor

from pepsicode.anthropic_adapter import ContextOverflowError
from pepsicode.context_manager import ContextManager
from pepsicode.logging_config import get_logger
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.types import AgentStep, ChatMessage, ModelAdapter, StreamToken

logger = get_logger("agent_loop")

# Upper bound on concurrent read-only tool execution within a single batch.
MAX_PARALLEL_TOOLS = 8

# HISTORY_SNIP (context-compression layer 1): large tool outputs are the most
# common source of context bloat.  Once a tool result is no longer among the
# most recent few, we keep only its head and tail lines and replace the middle
# with a marker.  This is the cheapest compression layer - no LLM call.
SNIP_CHAR_THRESHOLD = 2000
SNIP_HEAD_LINES = 12
SNIP_TAIL_LINES = 8
SNIP_PROTECT_RECENT = 3  # never snip the N most recent tool results
_SNIP_MARKER = "[snipped"


def _snip_tool_outputs(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Trim oversized older tool_result outputs in place, keeping head+tail.

    Idempotent: already-snipped results stay small and are skipped on
    subsequent passes.  The most recent ``SNIP_PROTECT_RECENT`` tool results
    are left at full fidelity so the model sees what it just did.
    """
    tool_result_indices = [
        i for i, m in enumerate(messages) if m.get("role") == "tool_result"
    ]
    if len(tool_result_indices) <= SNIP_PROTECT_RECENT:
        return messages
    protected = set(tool_result_indices[-SNIP_PROTECT_RECENT:])
    for i in tool_result_indices:
        if i in protected:
            continue
        content = messages[i].get("content", "")
        if not isinstance(content, str) or len(content) <= SNIP_CHAR_THRESHOLD:
            continue
        if _SNIP_MARKER in content:
            continue
        lines = content.splitlines()
        if len(lines) <= SNIP_HEAD_LINES + SNIP_TAIL_LINES:
            # Long single block without many newlines: hard-trim by chars.
            head = content[: SNIP_CHAR_THRESHOLD // 2]
            tail = content[-SNIP_CHAR_THRESHOLD // 2 :]
            removed = len(content) - len(head) - len(tail)
            messages[i] = {
                **messages[i],
                "content": f"{head}\n[snipped {removed} chars]\n{tail}",
            }
            continue
        head = lines[:SNIP_HEAD_LINES]
        tail = lines[-SNIP_TAIL_LINES:]
        removed = len(lines) - SNIP_HEAD_LINES - SNIP_TAIL_LINES
        messages[i] = {
            **messages[i],
            "content": "\n".join([*head, f"[snipped {removed} lines]", *tail]),
        }
    return messages

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


def _format_diagnostics(stop_reason: str | None, block_types: list[str] | None, ignored_block_types: list[str] | None) -> str:
    parts: list[str] = []
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")
    if block_types:
        parts.append(f"blocks={','.join(block_types)}")
    if ignored_block_types:
        parts.append(f"ignored={','.join(ignored_block_types)}")
    return f" Diagnostics: {'; '.join(parts)}." if parts else ""


def _is_recoverable_thinking_stop(*, is_empty: bool, stop_reason: str | None, block_types: list[str] | None, ignored_block_types: list[str] | None) -> bool:
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


def _is_concurrency_safe(tools: ToolRegistry, tool_name: str) -> bool:
    tool = tools.find(tool_name)
    return bool(tool and getattr(tool, "concurrency_safe", False))


def _execute_calls_in_order(
    calls: list[dict],
    tools: ToolRegistry,
    context: ToolContext,
    on_tool_start: Callable[[str, dict], None] | None,
    on_tool_result: Callable[[str, str, bool], None] | None,
):
    """Execute tool calls, running consecutive concurrency-safe (read-only)
    calls in parallel while keeping side-effecting tools exclusive.

    Results are returned in the original call order so the message history
    stays deterministic regardless of which tool finished first.
    """
    results: list = [None] * len(calls)
    index = 0
    total = len(calls)
    while index < total:
        run = []
        cursor = index
        while cursor < total and _is_concurrency_safe(tools, calls[cursor]["toolName"]):
            run.append(cursor)
            cursor += 1

        if len(run) > 1:
            for k in run:
                if on_tool_start:
                    on_tool_start(calls[k]["toolName"], calls[k]["input"])
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_TOOLS, len(run))) as pool:
                future_by_index = {
                    k: pool.submit(tools.execute, calls[k]["toolName"], calls[k]["input"], context)
                    for k in run
                }
                for k in run:
                    results[k] = future_by_index[k].result()
                    if on_tool_result:
                        on_tool_result(calls[k]["toolName"], results[k].output, not results[k].ok)
            index = cursor
        else:
            k = index
            if on_tool_start:
                on_tool_start(calls[k]["toolName"], calls[k]["input"])
            results[k] = tools.execute(calls[k]["toolName"], calls[k]["input"], context)
            if on_tool_result:
                on_tool_result(calls[k]["toolName"], results[k].output, not results[k].ok)
            index += 1
    return results


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
    on_assistant_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    context_manager: ContextManager | None = None,
) -> list[ChatMessage]:
    current_messages = list(messages)
    saw_tool_result = False
    empty_response_retry_count = 0
    recoverable_thinking_retry_count = 0
    overflow_retry_count = 0
    tool_error_count = 0
    step = 0

    if context_manager:
        context_manager.messages = current_messages
        stats = context_manager.get_stats()
        logger.info("Context: %d tokens (%.0f%%), %d messages",
                   stats.total_tokens, stats.usage_percentage, stats.messages_count)

        # Auto-compact if usage is near the limit
        if context_manager.should_auto_compact():
            logger.warning("Context near limit, auto-compacting...")
            current_messages = context_manager.compact_messages()
            if on_assistant_message:
                on_assistant_message(context_manager.get_context_summary())

    while max_steps is None or step < max_steps:
        step += 1
        next_step: AgentStep
        # Layer 1 compression: trim oversized older tool outputs before the
        # model sees them.  Cheap and lossless for recent context.
        current_messages = _snip_tool_outputs(current_messages)
        try:
            next_step = model.next(current_messages)
        except KeyboardInterrupt:
            raise  # Let Ctrl-C propagate
        except ContextOverflowError as error:
            # API rejected the request as too large.  Compact and retry rather
            # than failing the turn (mirrors Claude Code's 400/413 handling).
            if context_manager is not None and overflow_retry_count < 3:
                overflow_retry_count += 1
                logger.warning("Context overflow (%s); compacting and retrying (attempt %d)", error, overflow_retry_count)
                context_manager.messages = current_messages
                current_messages = context_manager.compact_messages(force=True)
                if on_progress_message:
                    on_progress_message(context_manager.get_context_summary())
                step -= 1  # don't consume a step on a pure retry
                continue
            fallback = f"Context too large and could not be compacted further: {error}"
            logger.error("Context overflow, no recovery: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages
        except ConnectionError as error:
            fallback = f"Network error (connection failed or dropped): {error}"
            logger.error("Model API connection error: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages
        except TimeoutError as error:
            fallback = f"Model API timeout: {error}"
            logger.error("Model API timeout: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages
        except Exception as error:
            # Catch-all for unexpected errors (rate limit, auth, server 5xx, etc.)
            error_type = type(error).__name__
            fallback = f"Model API error ({error_type}): {error}"
            logger.error("Model API error (%s): %s", error_type, error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages

        # Record real token usage from the provider when available, so context
        # stats reflect what the model actually saw rather than an estimate.
        if context_manager is not None:
            usage = getattr(model, "last_usage", None)
            if isinstance(usage, dict):
                context_manager.update_usage(
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )

        if next_step.type == "assistant":
            is_empty = _is_empty_assistant_response(next_step.content)
            if not is_empty and _should_treat_assistant_as_progress(
                kind=getattr(next_step, 'kind', None),
                content=next_step.content,
                saw_tool_result=saw_tool_result,
            ):
                # Preserve thinking blocks before progress message
                thinking_blocks = getattr(next_step, 'thinkingBlocks', None)
                if thinking_blocks:
                    current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
                if on_progress_message:
                    on_progress_message(next_step.content)
                current_messages.append({"role": "assistant_progress", "content": next_step.content})
                current_messages.append(
                    {
                        "role": "user",
                        "content": (
                            NUDGE_AFTER_TOOL_RESULT
                            if saw_tool_result and getattr(next_step, 'kind', None) != "progress"
                            else NUDGE_CONTINUE
                        ),
                    }
                )
                continue

            diagnostics = next_step.diagnostics

            if _is_recoverable_thinking_stop(
                is_empty=is_empty,
                stop_reason=diagnostics.stopReason if diagnostics else None,
                block_types=diagnostics.blockTypes if diagnostics else None,
                ignored_block_types=diagnostics.ignoredBlockTypes if diagnostics else None,
            ) and recoverable_thinking_retry_count < 3:
                recoverable_thinking_retry_count += 1
                stop_reason = diagnostics.stopReason if diagnostics else None
                # Preserve thinking blocks from the interrupted response
                thinking_blocks = getattr(next_step, 'thinkingBlocks', None)
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
                        "content": (
                            RESUME_AFTER_PAUSE
                            if stop_reason == "pause_turn"
                            else RESUME_AFTER_MAX_TOKENS
                        ),
                    }
                )
                continue

            if is_empty and empty_response_retry_count < 2:
                empty_response_retry_count += 1
                current_messages.append(
                    {
                        "role": "user",
                        "content": (
                            NUDGE_AFTER_EMPTY_RESPONSE
                            if saw_tool_result
                            else NUDGE_AFTER_EMPTY_NO_TOOLS
                        ),
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
                thinking_blocks = getattr(next_step, 'thinkingBlocks', None)
                if thinking_blocks:
                    current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
                current_messages.append({"role": "assistant", "content": fallback})
                return current_messages

            # Preserve thinking blocks before final assistant response
            thinking_blocks = getattr(next_step, 'thinkingBlocks', None)
            if thinking_blocks:
                current_messages.append({"role": "assistant_thinking", "blocks": thinking_blocks})
            if on_assistant_message:
                on_assistant_message(next_step.content)
            current_messages.append({"role": "assistant", "content": next_step.content})
            return current_messages

        # Preserve thinking blocks before tool call messages (always, even without content)
        thinking_blocks = getattr(next_step, 'thinkingBlocks', None)
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
                            "content": NUDGE_CONTINUE,
                        }
                    )
            else:
                if on_assistant_message:
                    on_assistant_message(next_step.content)
                current_messages.append({"role": role, "content": next_step.content})

        if not next_step.calls and next_step.content and next_step.contentKind != "progress":
            return current_messages

        for call in next_step.calls:
            current_messages.append(
                {
                    "role": "assistant_tool_call",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "input": call["input"],
                }
            )

        context = ToolContext(cwd=cwd, permissions=permissions)
        results = _execute_calls_in_order(
            next_step.calls,
            tools,
            context,
            on_tool_start,
            on_tool_result,
        )

        await_user_result = None
        for call, result in zip(next_step.calls, results):
            saw_tool_result = True
            if not result.ok:
                tool_error_count += 1
            current_messages.append(
                {
                    "role": "tool_result",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "content": result.output,
                    "isError": not result.ok,
                }
            )
            if result.awaitUser and await_user_result is None:
                await_user_result = result

        if await_user_result is not None:
            if on_assistant_message:
                on_assistant_message(await_user_result.output)
            current_messages.append({"role": "assistant", "content": await_user_result.output})
            return current_messages

    fallback = "Reached the maximum tool step limit for this turn."
    if on_assistant_message:
        on_assistant_message(fallback)
    current_messages.append({"role": "assistant", "content": fallback})
    return current_messages


# ---------------------------------------------------------------------------
# Streaming agent turn
# ---------------------------------------------------------------------------


def _accumulate_stream_tokens(
    token_stream: Generator[StreamToken, None, None],
    *,
    on_token: Callable[[StreamToken], None] | None = None,
) -> tuple[str, list[dict[str, any]] | None]:
    """Consume the token stream and accumulate text + tool-call blocks.

    Returns ``(text_content, tool_calls | None)``.  When the response contains
    only plain text, ``tool_calls`` is ``None``.  When a tool-use block is
    present, the list contains dicts with keys ``id``, ``toolName``, and
    ``input`` (the fully-assembled JSON string, parsed to a dict).

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

    for token in token_stream:
        if on_token is not None:
            on_token(token)

        if token.type == "text":
            text_parts.append(token.content)

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
    on_assistant_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    context_manager: ContextManager | None = None,
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
    saw_tool_result = False
    empty_response_retry_count = 0
    overflow_retry_count = 0
    tool_error_count = 0
    step = 0

    if context_manager:
        context_manager.messages = current_messages
        stats = context_manager.get_stats()
        logger.info(
            "Context: %d tokens (%.0f%%), %d messages",
            stats.total_tokens,
            stats.usage_percentage,
            stats.messages_count,
        )
        if context_manager.should_auto_compact():
            logger.warning("Context near limit, auto-compacting...")
            current_messages = context_manager.compact_messages()
            if on_assistant_message:
                on_assistant_message(context_manager.get_context_summary())

    while max_steps is None or step < max_steps:
        step += 1
        current_messages = _snip_tool_outputs(current_messages)

        # ------ Stream the model response ------
        text_content: str = ""
        parsed_calls: list[dict] | None = None
        try:
            # For the first step we use next_stream(); retries of the same step
            # fall back to the non-streaming path so error handling stays simple.
            if step == 1 or True:  # always stream for now
                token_stream = model.next_stream(current_messages)
                text_content, parsed_calls = _accumulate_stream_tokens(
                    token_stream,
                    on_token=on_token,
                )
            else:
                # Fallback non-stream (kept as reference; can be removed later)
                next_step = model.next(current_messages)
                text_content = next_step.content
                parsed_calls = [
                    {"id": c["id"], "toolName": c["toolName"], "input": c.get("input")}
                    for c in next_step.calls
                ] if next_step.calls else None

        except KeyboardInterrupt:
            raise
        except ContextOverflowError as error:
            if context_manager is not None and overflow_retry_count < 3:
                overflow_retry_count += 1
                logger.warning(
                    "Context overflow (%s); compacting and retrying (attempt %d)",
                    error, overflow_retry_count,
                )
                context_manager.messages = current_messages
                current_messages = context_manager.compact_messages(force=True)
                if on_progress_message:
                    on_progress_message(context_manager.get_context_summary())
                step -= 1
                continue
            fallback = f"Context too large and could not be compacted further: {error}"
            logger.error("Context overflow, no recovery: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages

        except ConnectionError as error:
            fallback = f"Network error (connection failed or dropped): {error}"
            logger.error("Model API connection error: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages

        except TimeoutError as error:
            fallback = f"Model API timeout: {error}"
            logger.error("Model API timeout: %s", error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages

        except Exception as error:
            error_type = type(error).__name__
            fallback = f"Model API error ({error_type}): {error}"
            logger.error("Model API error (%s): %s", error_type, error)
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages

        # ------ Record token usage ------
        if context_manager is not None:
            usage = getattr(model, "last_usage", None)
            if isinstance(usage, dict):
                context_manager.update_usage(
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )

        # ------ Handle empty responses ------
        is_empty = len(text_content.strip()) == 0 and not parsed_calls

        if is_empty and empty_response_retry_count < 2:
            empty_response_retry_count += 1
            current_messages.append(
                {
                    "role": "user",
                    "content": (
                        NUDGE_AFTER_EMPTY_RESPONSE if saw_tool_result
                        else NUDGE_AFTER_EMPTY_NO_TOOLS
                    ),
                }
            )
            continue

        if is_empty:
            fallback = "Model returned an empty response and the turn was stopped."
            if on_assistant_message:
                on_assistant_message(fallback)
            current_messages.append({"role": "assistant", "content": fallback})
            return current_messages

        # ------ Handle tool calls ------
        if parsed_calls:
            # Emit the assistant text as progress (if any) before tool calls.
            if text_content.strip():
                if on_progress_message:
                    on_progress_message(text_content)
                current_messages.append({"role": "assistant_progress", "content": text_content})

            # Record the tool-call messages.
            for call in parsed_calls:
                current_messages.append({
                    "role": "assistant_tool_call",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "input": call.get("input"),
                })

            context = ToolContext(cwd=cwd, permissions=permissions)
            results = _execute_calls_in_order(
                parsed_calls, tools, context, on_tool_start, on_tool_result,
            )

            await_user_result = None
            for call, result in zip(parsed_calls, results):
                saw_tool_result = True
                if not result.ok:
                    tool_error_count += 1
                current_messages.append({
                    "role": "tool_result",
                    "toolUseId": call["id"],
                    "toolName": call["toolName"],
                    "content": result.output,
                    "isError": not result.ok,
                })
                if result.awaitUser and await_user_result is None:
                    await_user_result = result

            if await_user_result is not None:
                if on_assistant_message:
                    on_assistant_message(await_user_result.output)
                current_messages.append({"role": "assistant", "content": await_user_result.output})
                return current_messages

            # Continue to the next agent step after tool execution.
            continue

        # ------ Plain text final answer ------
        if on_assistant_message:
            on_assistant_message(text_content)
        current_messages.append({"role": "assistant", "content": text_content})
        return current_messages

    fallback = "Reached the maximum tool step limit for this turn."
    if on_assistant_message:
        on_assistant_message(fallback)
    current_messages.append({"role": "assistant", "content": fallback})
    return current_messages
