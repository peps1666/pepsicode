"""Broker model decisions and tool batches for the M2 process driver."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from pepsicode.agents.status import STEP_LIMIT_MESSAGE
from pepsicode.context.context_artifacts import ContextArtifactStore, prepare_tool_result
from pepsicode.core.agent_loop import (
    _emit_hook,
    _execute_calls_in_order,
    _finish_hook_turn,
    _messages_for_model,
    _notify_usage,
    _record_cost_usage,
    _tool_recovery_nudge,
)
from pepsicode.execution.runtime import current_scope, scoped_turn
from pepsicode.hooks import HookEvent
from pepsicode.orchestration.process_channel import WorkerProtocolError, run_worker_process
from pepsicode.python_runtime import project_python
from pepsicode.tooling import ToolContext

_DRIVER = Path(__file__).with_name("process_worker_child.py").resolve()
_DRIVER_SOURCE = _DRIVER.read_text(encoding="utf-8") if _DRIVER.is_file() else ""
_INCOMPLETE = {"max_tokens", "pause_turn", "stream_incomplete"}


def create_process_runner(*, run_id, attempt_id, generation, directories, sandbox=None, port_range=None):
    @scoped_turn
    def run(
        *,
        model,
        tools,
        messages,
        cwd,
        permissions=None,
        max_steps=15,
        cost_tracker=None,
        cost_limit_usd=None,
        on_usage=None,
        on_tool_event=None,
        hook_engine=None,
        agent_scope="subagent",
        memory=None,
        cancellation_event=None,
    ):
        scope = current_scope()
        if scope.parent_task_id is None or not agent_scope.startswith("subagent"):
            raise ValueError("A process Worker must run in a child execution scope")
        driver_source = _DRIVER.read_text(encoding="utf-8") if _DRIVER.is_file() else _DRIVER_SOURCE
        if not driver_source:
            raise ValueError("The process Worker driver source is unavailable")
        frozen = getattr(sys, "frozen", False)
        if not frozen and not _DRIVER.is_file():
            raise ValueError("The process Worker driver requires a source Python runtime")
        if type(max_steps) is not int or not 1 <= max_steps <= 127:
            raise ValueError("The process Worker requires between 1 and 127 model rounds")
        scope.workspace_boundary = str(Path(cwd).resolve())
        scope.worker_sandbox = sandbox
        scope.allow_background_processes = False
        scope.process_environment = {
            name: os.environ[name]
            for name in ("PATH", "SystemRoot", "WINDIR", "SystemDrive", "PATHEXT")
            if name in os.environ
        }
        scope.process_environment.update(
            TMP=directories["tmp"],
            TEMP=directories["tmp"],
            TMPDIR=directories["tmp"],
            PIP_CACHE_DIR=str(Path(directories["dependencies"]) / "pip-cache"),
            npm_config_cache=str(Path(directories["dependencies"]) / "npm-cache"),
            PYTHONPYCACHEPREFIX=str(Path(directories["build"]) / "python-cache"),
            PEPSI_TEST_DATA_DIR=str(directories.get("test-data", "")),
        )
        if isinstance(port_range, dict) and "start" in port_range and "end" in port_range:
            scope.process_environment.update(
                PEPSI_WORKER_PORT_START=str(port_range["start"]),
                PEPSI_WORKER_PORT_END=str(port_range["end"]),
            )
        history = list(messages)
        artifacts = ContextArtifactStore.for_workspace(cwd)
        round_number = 0
        status = None
        calls = []
        context = ToolContext(
            cwd,
            permissions=permissions,
            execution=scope,
            cancellation_event=cancellation_event,
            hooks=hook_engine,
            agent_scope=agent_scope,
            memory=memory,
        )

        def finish(value, text):
            nonlocal status
            status = value
            history.append(
                {"role": "assistant", "content": text, **({"awaitingUser": True} if value == "awaiting_user" else {})}
            )
            return {"action": "done"}

        def advance():
            if round_number == max_steps:
                return finish("max_steps", STEP_LIMIT_MESSAGE)
            return {"action": "next"}

        def at_round(arguments, expected):
            value = arguments.get("round")
            return len(arguments) == 1 and type(value) is int and value == expected

        def next_model(active_scope, arguments):
            nonlocal round_number, calls
            if active_scope is not scope or calls or status or not at_round(arguments, round_number + 1):
                raise WorkerProtocolError("Worker requested a model round out of sequence")
            round_number += 1
            step = model.next(
                _messages_for_model(
                    history,
                    hook_engine,
                    cwd=cwd,
                    permissions=permissions,
                    tools=tools,
                    memory=memory,
                    runtime=getattr(model, "runtime", None),
                    worker_rounds_left=max_steps - round_number + 1,
                )
            )
            _record_cost_usage(model, cost_tracker)
            _notify_usage(model, on_usage)
            if step.diagnostics and step.diagnostics.stopReason in _INCOMPLETE:
                return finish("error", f"Worker model response was incomplete ({step.diagnostics.stopReason}).")
            if step.thinkingBlocks:
                history.append({"role": "assistant_thinking", "blocks": step.thinkingBlocks})
            calls = list(step.calls)
            if calls:
                if step.content:
                    history.append({"role": "assistant_progress", "content": step.content})
                return {"action": "tools"}
            if step.kind == "progress" or step.contentKind == "progress":
                history.append({"role": "assistant_progress", "content": step.content})
                return advance()
            if not step.content.strip():
                return finish("error", "Worker model returned an empty response.")
            return finish("completed", step.content)

        def execute_tools(active_scope, arguments):
            nonlocal calls
            if active_scope is not scope or not calls or status or not at_round(arguments, round_number):
                raise WorkerProtocolError("Worker requested a tool batch out of sequence")
            approved_calls, calls = calls, []
            for call in approved_calls:
                history.append(
                    {
                        "role": "assistant_tool_call",
                        "toolUseId": call["id"],
                        "toolName": call["toolName"],
                        "input": call["input"],
                    }
                )
            results = _execute_calls_in_order(approved_calls, tools, context, None, None, on_tool_event)
            for call, result in zip(approved_calls, results, strict=True):
                tool = tools.find(call["toolName"])
                history.append(
                    {
                        "role": "tool_result",
                        "toolUseId": call["id"],
                        "toolName": call["toolName"],
                        "content": prepare_tool_result(
                            tool_name=call["toolName"],
                            output=result.output,
                            budget_chars=tool.max_result_size_chars if tool else 4000,
                            artifact_store=artifacts,
                        ),
                        "isError": not result.ok,
                        **call.get("_telemetry", {}),
                    }
                )
            waiting = next((result for result in results if result.awaitUser), None)
            if waiting:
                return finish("awaiting_user", waiting.output)
            if guidance := _tool_recovery_nudge(approved_calls, results):
                history.append(guidance)
            return advance()

        _emit_hook(
            hook_engine,
            HookEvent.SUBAGENT_START,
            cwd=cwd,
            permissions=permissions,
            agent_scope=agent_scope,
            data={"message_count": len(history)},
        )
        if sandbox:
            driver = ["/usr/bin/python3", "-I", "-u", "-c", driver_source]
        elif frozen:
            interpreter = project_python(cwd)
            if Path(interpreter).resolve() == Path(sys.executable).resolve() or (
                not Path(interpreter).is_file() and shutil.which(interpreter) is None
            ):
                raise ValueError("The process Worker driver requires a usable Python interpreter")
            driver = [interpreter, "-I", "-u", "-c", driver_source]
        else:
            driver = [sys.executable, "-I", "-u", str(_DRIVER)]
        result = run_worker_process(
            driver,
            scope=scope,
            cwd=cwd,
            run_id=run_id,
            attempt_id=attempt_id,
            generation=generation,
            payload={"max_rounds": max_steps},
            operations={"next": next_model, "tools": execute_tools},
        )
        if status is None or result.payload:
            raise WorkerProtocolError("Worker exited without a host-confirmed result")
        return _finish_hook_turn(
            history,
            hook_engine,
            cwd=cwd,
            permissions=permissions,
            agent_scope=agent_scope,
            status=status,
            error=history[-1]["content"] if status == "error" else "",
        )

    return run
