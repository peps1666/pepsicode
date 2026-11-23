"""Turn execution and service-mode turn notifications for PepsiCodeServer."""

from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from pepsicode.cli.cli_commands import expand_prompt_command
from pepsicode.core.agent_loop import run_agent_turn_stream
from pepsicode.core.session import save_session
from pepsicode.core.session_tree import format_tree, prepare_regeneration, record_messages, recording_session
from pepsicode.core.verification import run_verified_turn, snapshot_verification_state
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.providers import model_settings_snapshot
from pepsicode.logging_config import get_logger
from pepsicode.server import protocol
from pepsicode.server.session_runtime import ClientSession, app_attr
from pepsicode.server.thinking import ThinkingStream
from pepsicode.service.policy import check_policy_checkpoint
from pepsicode.types import ChatMessage
from pepsicode.usage import usage_snapshot

logger = get_logger("server")


def record_service_turn_notifications(server: Any, session: ClientSession, task: dict[str, Any] | None) -> None:
    """Queue outbox notifications for unanswered questions and task deliveries."""
    host = server.service_host
    if host is None or session.session_data is None:
        return
    sid = session.session_data.session_id
    control = session.session_data.task_control or {}
    questions = [
        q for q in control.get("questions", []) if isinstance(q, dict) and q.get("status") != "answered" and q.get("id")
    ]
    for q in questions:
        host.notify(
            dedup_key=f"question:{sid}:{q['id']}",
            title=f"Question awaiting reply ({q['id']})",
            body=str(q.get("question", ""))[:300],
        )
    orchestration = control.get("orchestration") or {}
    deliveries = [d for d in (orchestration.get("deliveries") or {}).values() if isinstance(d, dict)]
    delivered_info = ((task or {}).get("acceptance") or {}).get("delivered") or {}
    if (task and task.get("status") == "complete") or deliveries:
        task_id = (task or {}).get("id", "task")
        revision = (task or {}).get("revision", 1)
        version_key = (
            delivered_info.get("head_sha")
            or (deliveries[-1].get("head_sha") if deliveries else "")
            or (task or {}).get("status", "delivered")
        )
        evidence_count = len((task or {}).get("evidence") or [])
        uncertain_ids = [d.get("delivery_id", "") for d in deliveries if d.get("status") == "uncertain"]
        pending_actions = [f"question:{q['id']}" for q in questions] + [
            f"reconcile:{did}" for did in uncertain_ids if did
        ]
        retained_envs = sum(
            1
            for env in (orchestration.get("environments") or {}).values()
            if isinstance(env, dict) and env.get("state") != "pruned"
        )
        body = (
            f"status={(task or {}).get('status', 'delivered')}; "
            f"version={str(version_key)[:12]}; "
            f"evidence={evidence_count}; "
            f"pending_actions={','.join(pending_actions) or 'none'}; "
            f"retained_environments={retained_envs}"
        )
        host.notify(
            dedup_key=f"delivery:{sid}:{task_id}:r{revision}:{version_key}",
            title=f"Task delivery summary ({task_id})",
            body=body[:500],
        )


async def run_turn_background(
    server: Any,
    req_id: int | str,
    params: dict[str, Any],
    session: ClientSession,
    emit: Any,
    websocket: Any,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Run a turn in the background and send the RPC response when complete."""
    try:
        result = await run_turn(server, params, session, emit, loop)
        response = protocol.make_response(req_id, result=result)
    except Exception as error:  # noqa: BLE001
        logger.exception("Error in turn/run")
        session.input_queue.close()
        await emit(
            "turn/end",
            {
                "status": "error",
                "error": str(error),
                "task": session.session_data.task_control.get("task") if session.session_data else None,
            },
        )
        response = protocol.make_response(req_id, error=protocol.make_error(protocol.ERR_INTERNAL, str(error)))
    finally:
        session.input_queue.close()
    if websocket is not None:
        try:
            await websocket.send(json.dumps(response, ensure_ascii=False, default=str))
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to send turn/run response: %s", error)


async def run_turn(
    server: Any,
    params: dict[str, Any],
    session: ClientSession,
    emit: Any,
    loop: asyncio.AbstractEventLoop,
) -> dict[str, Any]:
    """Run an agent turn in a worker thread, streaming events back."""
    if params.get("task_kind") is not None and params["task_kind"] not in {"answer", "change", "operation"}:
        raise ValueError("task_kind must be answer, change, or operation")
    if params.get("acceptance") is not None:
        from pepsicode.core.task_acceptance import validate_criteria

        validate_criteria(params["acceptance"])
    answer_to = params.get("answer_to", [])
    if params.get("task_requirements") is not None:
        from pepsicode.core.task_acceptance import validate_requirements

        validate_requirements(params["task_requirements"])
    if not isinstance(answer_to, list) or not all(isinstance(item, str) and item for item in answer_to):
        raise ValueError("answer_to must be an array of question IDs")
    if session.model is None:
        raise RuntimeError("Session not initialized. Call session/create first.")
    runtime: dict[str, Any] | None = session.runtime
    if not params.get("_orchestration_client", True) and (runtime or {}).get("orchestration", {}).get("enabled"):
        orchestration = session.session_data.task_control.get("orchestration", {}) if session.session_data else {}
        if server.service_mode or any(row["state"] != "settled" for row in orchestration.get("effects", [])):
            raise ValueError("This client does not support asynchronous orchestration; update the client")
        base_runtime = dict(runtime or {})
        runtime = {**base_runtime, "orchestration": {**base_runtime["orchestration"], "enabled": False}}
    host = server.service_host
    if host is not None:
        if not host.storage_ok():
            raise ValueError("Service event storage is failing; new work is paused until the operator clears the fault")
        policy, effective, policy_reason = host.policy_effective()
        if not effective:
            raise PermissionError(f"Unattended policy is not effective: {policy_reason}")
        runtime = dict(runtime or {})

        def check_service_policy(**checkpoint_context: Any) -> None:
            check_policy_checkpoint(host.load_policy(), **checkpoint_context)

        runtime["_servicePolicyCheckpoint"] = check_service_policy
        for setting, key in (
            ("modelRequestLimit", "max_requests"),
            ("budgetLimitUsd", "max_cost_usd"),
            ("taskTimeout", "max_duration_seconds"),
        ):
            cap = policy["limits"].get(key)
            if cap is not None:
                configured = runtime.get(setting)
                if configured is None and setting == "modelRequestLimit":
                    configured = 200
                runtime[setting] = cap if configured is None else min(cap, configured)

    permissions = session.require_permissions()
    async with session._turn_lock:
        if session._closing:
            raise ValueError("Connection is closing")
        if session._cancellation_event.is_set():
            await emit("turn/end", {"status": "cancelled"})
            return {"status": "cancelled", "messages": len(session.messages)}
        if session._worker_active.is_set():
            raise ValueError("The previous turn is still finishing")
        user_input = params.get("input", "").strip()
        if not answer_to and user_input.partition(" ")[0] == "/answer":
            parts = user_input.split(maxsplit=2)
            if len(parts) != 3:
                raise ValueError("Usage: /answer <question-id> <reply>")
            answer_to, user_input = [parts[1]], parts[2]
        regeneration = not answer_to and ("regenerate_entry_id" in params or user_input == "/retry")
        regenerated_prompt = None
        if regeneration:
            session.snapshot()
            data = session.require_session_data()
            regenerated_prompt = prepare_regeneration(data, params.get("regenerate_entry_id"))
            session.messages = list(cast(list[ChatMessage], data.messages))
            if session.context_manager:
                session.context_manager.select_branch(
                    cast(list[dict[str, Any]], session.messages), session.cwd, session.permissions
                )
            user_input = regenerated_prompt.get("displayContent", regenerated_prompt["content"])
        if not user_input:
            raise ValueError("Missing 'input' parameter")

        if not answer_to and not regeneration and user_input.split()[0] in {"/tree", "/fork", "/clone"}:
            parts = user_input.split()
            if len(parts) > (1 if parts[0] == "/clone" else 2):
                raise ValueError("Usage: /tree [entry-id], /fork [user-entry-id], /clone")
            if len(parts) == 1 and parts[0] != "/clone":
                session.snapshot()
                output = format_tree(session.require_session_data(), users_only=parts[0] == "/fork")
                await emit("message/start", {})
                await emit("message/end", {"content": output})
                await emit("turn/end", {"status": "ok"})
                return {"output": output}
            method = {"/tree": "session/checkout", "/fork": "session/fork", "/clone": "session/clone"}[parts[0]]
            branch_result: dict[str, Any] = await server._change_branch(
                method, {"entry_id": parts[1] if len(parts) > 1 else None}, session, emit
            )
            await emit("turn/end", {"status": "ok"})
            return branch_result

        prompt_message = None if answer_to else regenerated_prompt or expand_prompt_command(user_input, session.cwd)
        if user_input.startswith("/") and prompt_message is None and not answer_to:
            cmd_result: dict[str, Any] = await server._handle_local_command(user_input, session, emit)
            content = cmd_result.get("output")
            if cmd_result.get("status") == "unknown_command":
                content = f"Unknown command: {user_input.split()[0]}. Use /help or /commands."
            if cmd_result.get("status") == "plan_mode":
                content = f"Plan mode active. Plan file: {cmd_result['plan_file']}"
            if content:
                await emit("message/start", {})
                await emit(
                    "message/end", {"content": content, "is_error": cmd_result.get("status") == "unknown_command"}
                )
            await emit("turn/end", {"status": "ok"})
            return cmd_result

        if answer_to:
            from pepsicode.core.task_control import pending_questions

            data = session.require_session_data()
            with recording_session(data):
                control = data.task_control
                task = control.get("task") or {}
                if not set(answer_to) <= {q["id"] for q in pending_questions(control, task.get("id"))}:
                    raise ValueError("Question is no longer pending in the current task")
        session.messages.append(cast(ChatMessage, prompt_message or {"role": "user", "content": user_input}))
        if answer_to:
            cast(dict[str, Any], session.messages[-1]).setdefault("source", {"kind": "human", "id": uuid.uuid4().hex})[
                "answerTo"
            ] = answer_to
        if session.session_data is not None:
            record_messages(session.session_data, cast(list[dict[str, Any]], session.messages))
        if regeneration:
            if session.runtime is not None:
                session.runtime.pop("_lastVerification", None)
            await emit("session/view", session.conversation_view())
        if not session.input_queue.accepting:
            if session.session_data is not None:
                session.input_queue.bind_session(session.session_data)

            def on_queue_update(update: dict[str, Any]) -> None:
                asyncio.run_coroutine_threadsafe(emit("input/queue", update), loop)

            session.input_queue.start(on_queue_update)
        if host is not None and session.session_data is not None:
            host.flush_pending_external(session.session_data.session_id)
        permissions.begin_turn()
        session._rebuild_system_prompt()
        if session.context_manager is not None:
            session.context_manager.messages = list(cast(list[dict[str, Any]], session.messages))
        await emit("usage/update", usage_snapshot(session.context_manager, None))

        # Token streaming: accumulate and emit deltas
        message_started = False
        thinking = ThinkingStream(lambda name, data: asyncio.run_coroutine_threadsafe(emit(name, data), loop))

        def on_token(token: Any) -> None:
            nonlocal message_started
            thinking.consume(token)
            if token.type == "usage":
                asyncio.run_coroutine_threadsafe(
                    emit("usage/update", usage_snapshot(session.context_manager, token.usage)), loop
                )
                return
            if token.type != "text":
                return
            if not message_started:
                message_started = True
                asyncio.run_coroutine_threadsafe(emit("message/start", {}), loop)
            asyncio.run_coroutine_threadsafe(emit("message/delta", {"text": token.content}), loop)

        def on_tool_event(event_name: str, data: dict[str, Any]) -> None:
            thinking.finish()
            if isinstance(data.get("output"), str):
                data = {**data, "output": data["output"][:10000]}
            asyncio.run_coroutine_threadsafe(emit(event_name, data), loop)

        def on_tool_start(tool_name: str, tool_input: dict[str, Any]) -> None:
            asyncio.run_coroutine_threadsafe(
                emit("tool/call", {"tool": tool_name, "input": tool_input, "status": "running"}), loop
            )

        def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
            asyncio.run_coroutine_threadsafe(
                emit(
                    "tool/result",
                    {"tool": tool_name, "output": output[:5000], "is_error": is_error, "status": "done"},
                ),
                loop,
            )

        def on_assistant_message(content: str) -> None:
            finish_assistant_message(content, is_error=False)

        def on_error_message(content: str) -> None:
            finish_assistant_message(content, is_error=True)

        def finish_assistant_message(content: str, *, is_error: bool) -> None:
            nonlocal message_started
            thinking.finish("interrupted" if is_error else "completed")
            if not message_started:
                message_started = True
                asyncio.run_coroutine_threadsafe(emit("message/start", {}), loop)
            asyncio.run_coroutine_threadsafe(emit("message/end", {"content": content, "is_error": is_error}), loop)
            message_started = False

        def on_progress_message(content: str) -> None:
            nonlocal message_started
            thinking.finish("cancelled" if session._cancellation_event.is_set() else "completed")
            if message_started:
                asyncio.run_coroutine_threadsafe(emit("message/end", {"content": content, "role": "progress"}), loop)
                message_started = False
            else:
                asyncio.run_coroutine_threadsafe(emit("progress/message", {"content": content}), loop)

        def on_usage(usage: dict[str, Any]) -> None:
            asyncio.run_coroutine_threadsafe(emit("cost/update", session.cost_snapshot()), loop)
            asyncio.run_coroutine_threadsafe(emit("usage/update", usage_snapshot(session.context_manager, usage)), loop)

        runner = app_attr("run_agent_turn_stream", run_agent_turn_stream)
        verified_runner = app_attr("run_verified_turn", run_verified_turn)

        def run_turn_sync() -> list[ChatMessage]:
            try:
                with recording_session(session.session_data):
                    result = verified_runner(
                        runtime=runtime,
                        task_kind=params.get("task_kind"),
                        acceptance=params.get("acceptance"),
                        task_requirements=params.get("task_requirements"),
                        runner=runner,
                        input_queue=session.input_queue,
                        cancellation_event=session._cancellation_event,
                        on_verification=lambda report: asyncio.run_coroutine_threadsafe(
                            emit("verification/update", report), loop
                        ),
                        model=session.model,
                        tools=session.tools,
                        messages=list(session.messages),
                        cwd=session.cwd,
                        permissions=permissions,
                        context_manager=session.context_manager,
                        cost_tracker=session.cost_tracker,
                        on_token=on_token,
                        on_tool_event=on_tool_event,
                        on_tool_start=on_tool_start,
                        on_tool_result=on_tool_result,
                        on_assistant_message=on_assistant_message,
                        on_error_message=on_error_message,
                        on_progress_message=on_progress_message,
                        on_usage=on_usage,
                        hook_engine=session.hook_engine,
                        memory=session.memory_manager,
                    )
                    return cast(list[ChatMessage], result)
            finally:
                thinking.finish("cancelled" if session._cancellation_event.is_set() else "interrupted")
                session._worker_active.clear()

        executor = ThreadPoolExecutor(max_workers=1)
        session._worker_active.set()
        try:
            future = loop.run_in_executor(executor, run_turn_sync)
        except Exception:
            session._worker_active.clear()
            executor.shutdown(wait=False)
            raise
        turn_future = asyncio.wrap_future(future)
        session._current_turn_task = turn_future

        try:
            new_messages = await turn_future
            session.messages = new_messages
            session_runtime = session.runtime
            if runtime is not session_runtime and session_runtime is not None and runtime is not None:
                for key in ("_lastExecution", "_lastVerification"):
                    if key in runtime:
                        session_runtime[key] = runtime[key]
                    else:
                        session_runtime.pop(key, None)
        except asyncio.CancelledError:
            await emit("turn/end", {"status": "cancelled"})
            return {"status": "cancelled", "messages": len(session.messages)}
        finally:
            permissions.end_turn()
            executor.shutdown(wait=False)

        # Persist session
        if session.session_data is not None:
            session.session_data.messages = list(cast(list[dict[str, Any]], session.messages))
            session.session_data.verification_state = snapshot_verification_state(session.tools, session.runtime)
            session.session_data.permission_mode = permissions.mode.value
            session.session_data.access_mode = permissions.access_mode.value
            session.session_data.plan_file_path = permissions.plan_file_path
            if session.runtime and not isinstance(session.model, app_attr("MockModelAdapter", MockModelAdapter)):
                session.session_data.model_settings = model_settings_snapshot(session.runtime)
            try:
                save_session(session.session_data)
                await emit("session/saved", {"session_id": session.session_data.session_id})
            except Exception as error:  # noqa: BLE001
                logger.warning("Failed to save session: %s", error)

        if session.session_data is not None:
            await emit("session/view", session.conversation_view())

        execution = (
            (session.runtime or {}).get("_lastExecution")
            or (session.session_data.execution_state if session.session_data else None)
            or {}
        )
        turn_status = execution.get("state", "cancelled" if session._cancellation_event.is_set() else "completed")
        if turn_status == "succeeded":
            turn_status = "completed"
        task = session.session_data.task_control.get("task") if session.session_data else None
        if task and task["status"] not in {"complete", "answered", "active"}:
            turn_status = task["status"]
        if host is not None and session.session_data is not None:
            server._record_service_turn_notifications(session, task)
        await emit(
            "turn/end",
            {
                "status": turn_status,
                "execution": execution,
                "task": task,
                "messages": len(session.messages),
                "verification": (session.runtime or {}).get("_lastVerification"),
            },
        )

        last_assistant = next((m for m in reversed(session.messages) if m.get("role") == "assistant"), None)
        return {
            "status": turn_status,
            "messages": len(session.messages),
            "last_message": last_assistant.get("content", "") if last_assistant else "",
        }
